import datetime
import json
import os
import re
from content_resolver.data_generation import _generate_json_file
from content_resolver.utils import dump_data, err_log, log


def _save_current_historic_data_daily(query):
    # Daily historic data for charts
    # Package lists are saved separately by _save_current_historic_data_daily_pkgs

    log("Generating current daily historic data...")

    # Where to save it
    now = datetime.datetime.now()
    day_of_year = now.strftime("%j")
    year = now.strftime("%Y")
    filename = "historic_data-{year}-day_{day}.json".format(
        year=year,
        day=day_of_year
    )
    output_dir = os.path.join(query.settings["output"], "history")
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, filename)

    # What to save there
    history_data = {
        "date": str(now.strftime("%Y-%m-%d")),
        "workloads": {},
        "envs": {},
        "repos": {},
        "views": {},
    }

    # Workloads
    for workload_id in query.workloads(None,None,None,None,list_all=True):
        workload = query.data["workloads"][workload_id]

        if not workload["succeeded"]:
            continue

        workload_history = {
            "size": query.workload_size_id(workload_id),
            "pkg_count": len(query.workload_pkgs_id(workload_id)),
        }

        history_data["workloads"][workload_id] = workload_history

    # Environments
    for env_id in query.envs(None,None,None,list_all=True):
        env = query.data["envs"][env_id]

        if not env["succeeded"]:
            continue

        env_history = {
            "size": query.env_size_id(env_id),
            "pkg_count": len(query.env_pkgs_id(env_id)),
        }

        history_data["envs"][env_id] = env_history

    # Repositories
    for repo_id in query.configs["repos"].keys():
        history_data["repos"][repo_id] = {}

        for arch, pkgs in query.data["pkgs"][repo_id].items():

            repo_history = {
                "pkg_count": len(pkgs),
            }

            history_data["repos"][repo_id][arch] = repo_history

    # Views
    for view_conf_id, view_conf in query.configs["views"].items():
        view_all_arches = query.data["views_all_arches"][view_conf_id]

        view_data = {
            "srpm_count_env": view_all_arches["numbers"]["srpms"]["env"],
            "srpm_count_req": view_all_arches["numbers"]["srpms"]["req"],
            "srpm_count_dep": view_all_arches["numbers"]["srpms"]["dep"],
        }

        if view_all_arches["has_buildroot"]:
            view_data["srpm_count_build_base"] = view_all_arches["numbers"]["srpms"]["build_base"]
            view_data["srpm_count_build_level_1"] = view_all_arches["numbers"]["srpms"]["build_level_1"]
            view_data["srpm_count_build_level_2_plus"] = view_all_arches["numbers"]["srpms"]["build_level_2_plus"]

        history_data["views"][view_conf_id] = view_data

    # And save it
    log(f"  Saving in: {file_path}")
    dump_data(file_path, history_data)

    log("  Done!")
    log("")


def _save_current_historic_data_daily_pkgs(query):
    # Daily package lists for browse and compare
    # Kept separate from the daily counts so package list snapshots can be
    # deleted for space, if needed, without impacting the chart data

    log("Generating current daily package lists...")

    # Where to save it
    now = datetime.datetime.now()
    day_of_year = now.strftime("%j")
    year = now.strftime("%Y")
    filename = "historic_data_pkgs-{year}-day_{day}.json".format(
        year=year,
        day=day_of_year
    )
    output_dir = os.path.join(query.settings["output"], "history")
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, filename)

    # What to save there
    history_pkgs = {
        "date": str(now.strftime("%Y-%m-%d")),
        "views": {},
    }

    for view_conf_id, view_conf in query.configs["views"].items():

        # Sets remove duplicates across arches
        all_source_names = set()
        all_source_nevrs = set()
        runtime_source_names = set()
        runtime_source_nevrs = set()
        buildroot_source_names = set()
        buildroot_source_nevrs = set()

        for arch in view_conf["architectures"]:
            view_id = f"{view_conf_id}:{arch}"
            view = query.data["views"][view_id]

            for pkg_id, pkg in view["pkgs"].items():
                source_name = pkg["source_name"]
                source_nevr = pkg["sourcerpm"].rsplit(".src.rpm")[0]

                all_source_names.add(source_name)
                all_source_nevrs.add(source_nevr)

                # Runtime if in a workload, buildroot-only otherwise
                if pkg["in_workload_ids_all"]:
                    runtime_source_names.add(source_name)
                    runtime_source_nevrs.add(source_nevr)
                else:
                    buildroot_source_names.add(source_name)
                    buildroot_source_nevrs.add(source_nevr)

        # Sorted for consistent output
        view_pkgs = {
            "all_source_names": sorted(list(all_source_names)),
            "all_source_nevrs": sorted(list(all_source_nevrs)),
            "runtime_source_names": sorted(list(runtime_source_names)),
            "runtime_source_nevrs": sorted(list(runtime_source_nevrs)),
            "buildroot_source_names": sorted(list(buildroot_source_names)),
            "buildroot_source_nevrs": sorted(list(buildroot_source_nevrs)),
        }

        history_pkgs["views"][view_conf_id] = view_pkgs

    # And save it
    log(f"  Saving in: {file_path}")
    dump_data(file_path, history_pkgs)

    log("  Done!")
    log("")


def _read_historic_data(query):
    log("Reading historic data...")

    directory = os.path.join(query.settings["output"], "history")

    # Do some basic validation of the filename
    all_filenames = os.listdir(directory)
    valid_filenames = []
    for filename in all_filenames:
        if bool(re.match("historic_data-....-week_...json", filename)):
            valid_filenames.append(filename)
    valid_filenames.sort()

    # Get the data
    historic_data = {}

    for filename in valid_filenames:
        with open(os.path.join(directory, filename), "r") as file:
            try:
                document = json.load(file)

                date = datetime.datetime.strptime(document["date"],"%Y-%m-%d")
                year = date.strftime("%Y")
                week = date.strftime("%W")
                key = f"{year}-week_{week}"
            except (KeyError, ValueError):
                err_log(f"Invalid file in historic data: {filename}. Ignoring.")
                continue

            historic_data[key] = document

    return historic_data


def _read_historic_data_daily(query):
    # Read all daily snapshot files from output/history/

    log("Reading daily historic data...")

    directory = os.path.join(query.settings["output"], "history")

    # Filter for daily files only (not weekly)
    all_filenames = os.listdir(directory)
    valid_filenames = []
    for filename in all_filenames:
        if bool(re.match(r"historic_data-....-day_....json", filename)):
            valid_filenames.append(filename)
    valid_filenames.sort()

    # Get the data
    historic_data_daily = {}

    for filename in valid_filenames:
        with open(os.path.join(directory, filename), "r") as file:
            try:
                document = json.load(file)
                date = document["date"]
            except (KeyError, ValueError):
                err_log("Invalid file in daily historic data: {filename}. Ignoring.".format(
                    filename=filename
                ))
                continue

            historic_data_daily[date] = document

    return historic_data_daily


def _list_historic_data_daily_pkgs_dates(query):
    # Map date to filename for daily package list files
    # Only the filenames are read, the browser fetches the contents later

    directory = os.path.join(query.settings["output"], "history")

    dates = {}

    for filename in sorted(os.listdir(directory)):
        match = re.match(r"historic_data_pkgs-(....)-day_(...)\.json", filename)
        if not match:
            continue

        year, day_of_year = match.groups()

        try:
            date = datetime.datetime.strptime(
                "{year}-{day}".format(year=year, day=day_of_year), "%Y-%j"
            ).strftime("%Y-%m-%d")
        except ValueError:
            err_log("Invalid file in daily package lists: {filename}. Ignoring.".format(
                filename=filename
            ))
            continue

        dates[date] = filename

    return dates


def _generate_chartjs_data(historic_data, query):

    # Data for workload pages
    for workload_id in query.workloads(None, None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        workload = query.data["workloads"][workload_id]
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = workload_conf["name"]
        dataset["fill"] = "false"

        for _,entry in historic_data.items():
            try:
                size = entry["workloads"][workload_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = round(size/1024/1024, 1)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append(None)

        entry_data["datasets"].append(dataset)

        entry_name = f"chartjs-data--workload--{workload_id}"
        _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for workload overview pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for workload_id in query.workloads(workload_conf_id, None, repo_id, None, list_all=True):

                workload = query.data["workloads"][workload_id]
                env_conf_id = workload["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=workload["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["workloads"][workload_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = round(size/1024/1024, 1)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append(None)

                entry_data["datasets"].append(dataset)

            entry_name = f"chartjs-data--workload-overview--{workload_conf_id}--{repo_id}"
            _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for workload cmp arches pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for env_conf_id in query.workloads(workload_conf_id,None,None,None,output_change="env_conf_ids"):
            for repo_id in query.workloads(workload_conf_id,env_conf_id,None,None,output_change="repo_ids"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,env_conf_id,repo_id,None,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    env_conf_id = workload["env_conf_id"]
                    env_conf = query.configs["envs"][env_conf_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{arch}".format(
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = round(size/1024/1024, 1)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append(None)

                    entry_data["datasets"].append(dataset)

                entry_name = f"chartjs-data--workload-cmp-arches--{workload_conf_id}--{env_conf_id}--{repo_id}"
                _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for workload cmp envs pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            for arch in query.workloads(workload_conf_id,None,repo_id,None,output_change="arches"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,None,repo_id,arch,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    repo = query.configs["repos"][repo_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{repo} {arch}".format(
                        repo=repo["name"],
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = round(size/1024/1024, 1)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append(None)

                    entry_data["datasets"].append(dataset)

                entry_name = f"chartjs-data--workload-cmp-envs--{workload_conf_id}--{repo_id}--{arch}"
                _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for env pages
    for env_id in query.envs(None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        env = query.data["envs"][env_id]
        env_conf_id = env["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = env_conf["name"]
        dataset["fill"] = "false"


        for _,entry in historic_data.items():
            try:
                size = entry["envs"][env_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = round(size/1024/1024, 1)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append(None)

        entry_data["datasets"].append(dataset)

        entry_name = f"chartjs-data--env--{env_id}"
        _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for env overview pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id, repo_id, None, list_all=True):

                env = query.data["envs"][env_id]
                env_conf_id = env["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=env["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = round(size/1024/1024, 1)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append(None)

                entry_data["datasets"].append(dataset)

            entry_name = f"chartjs-data--env-overview--{env_conf_id}--{repo_id}"
            _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for env cmp arches pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            env_conf = query.configs["envs"][env_conf_id]
            env_conf = query.configs["envs"][env_conf_id]
            repo = query.configs["repos"][repo_id]

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id,repo_id,None,list_all=True):

                env = query.data["envs"][env_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "{arch}".format(
                    arch=env["arch"]
                )
                dataset["fill"] = "false"

                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = round(size/1024/1024, 1)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append(None)

                entry_data["datasets"].append(dataset)

            entry_name = f"chartjs-data--env-cmp-arches--{env_conf_id}--{repo_id}"
            _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for view pages 
    for view_conf_id in query.configs["views"].keys():
        view_all_arches = query.data["views_all_arches"][view_conf_id]

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []

        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        dataset_names = ["env", "req", "dep"]
        if view_all_arches["has_buildroot"]:
            dataset_names.extend(["build_base", "build_level_1", "build_level_2_plus"])

        dataset_metadata = {
            "env": {
                "name": "Environment",
                "color": "#ffc107"
            },
            "req": {
                "name": "Required",
                "color": "#28a745"
            },
            "dep": {
                "name": "Dependency",
                "color": "#6c757d"
            },
            "build_base": {
                "name": "Base Buildroot",
                "color": "#a39e87"
            },
            "build_level_1": {
                "name": "Buildroot level 1",
                "color": "#999"
            },
            "build_level_2_plus": {
                "name": "Buildroot levels 2+",
                "color": "#bbb"
            },
        }

        for dataset_name in dataset_names:
            dataset_key = f"srpm_count_{dataset_name}"

            dataset = {}
            dataset["data"] = []
            dataset["label"] = dataset_metadata[dataset_name]["name"]
            dataset["backgroundColor"] = dataset_metadata[dataset_name]["color"]

            loop_index = 0
            for _,entry in historic_data.items():
                try:
                    srpm_count = entry["views"][view_conf_id][dataset_key]

                    # It's a stack chart, so I need to show the numbers on top of each other
                    if dataset_name == "env":
                        srpm_count_compound = srpm_count
                    else:
                        srpm_count_compound = entry_data["datasets"][-1]["data"][loop_index] + srpm_count

                    dataset["data"].append(srpm_count_compound)
                except (KeyError, IndexError):
                    dataset["data"].append(None)

                loop_index += 1

            entry_data["datasets"].append(dataset)

        entry_name = f"chartjs-data--view--{view_conf_id}"
        _generate_json_file(entry_data, entry_name, query.settings)


def _merge_weekly_and_daily(historic_data_weekly, historic_data_daily):

    # Merge weekly and daily historic data as one continuous series for the chart
    
    merged = {}

    for entry in historic_data_weekly.values():
        # Keyed by date (not "YYYY-week_WW"), so both sort together
        merged[entry["date"]] = entry

    # Daily counts take priority (replaces any weekly snapshot sharing the same date)
    merged.update(historic_data_daily)

    return dict(sorted(merged.items()))


def generate_historic_data(query):
    log("")
    log("###############################################################################")
    log("### Historic Data #############################################################")
    log("###############################################################################")
    log("")

    # Step 1: Save current data
    _save_current_historic_data_daily(query)
    _save_current_historic_data_daily_pkgs(query)

    # Step 2: Read historic data
    historic_data_weekly = _read_historic_data(query)
    historic_data_daily = _read_historic_data_daily(query)
    historic_data = _merge_weekly_and_daily(historic_data_weekly, historic_data_daily)

    # Step 3: Generate Chart.js data
    _generate_chartjs_data(historic_data, query)

    # Date to filename map for the browser to fetch package lists
    query.data["historic_daily_dates"] = _list_historic_data_daily_pkgs_dates(query)

    log("Done!")
    log("")