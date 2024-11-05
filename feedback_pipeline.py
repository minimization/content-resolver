#!/usr/bin/python3

import datetime
from feedback_pipeline.analyzer import Analyzer
from feedback_pipeline.data_generation import generate_data_files
from feedback_pipeline.historia_data import generate_historic_data
from feedback_pipeline.page_generation import generate_pages
from feedback_pipeline.query import Query
from feedback_pipeline.utils import load_data, log, datetime_now_string, dump_data
from feedback_pipeline.config_manager import ConfigManager



# Features of this new release
# - multiarch from the ground up!
# - more resilient
# - better internal data structure
# - user-defined views


###############################################################################
### Help ######################################################################
###############################################################################


# Configs:
#   TYPE:           KEY:          ID:
# - repo            repos         repo_id
# - env_conf        envs          env_id
# - workload_conf   workloads     workload_id
# - label           labels        label_id
# - conf_view       views         view_id
#
# Data:
#   TYPE:         KEY:                 ID:
# - pkg           pkgs/repo_id/arch    NEVR
# - env           envs                 env_id:repo_id:arch_id
# - workload      workloads            workload_id:env_id:repo_id:arch_id
# - view          views                view_id:repo_id:arch_id
#
#
#




###############################################################################
### Main ######################################################################
###############################################################################


def main():

    # -------------------------------------------------
    # Stage 1: Data collection and analysis using DNF
    # -------------------------------------------------

    # measuring time of execution
    time_started = datetime_now_string()
    config_manager = ConfigManager()

    settings = config_manager.settings

    settings["global_refresh_time_started"] = datetime.datetime.now().strftime("%-d %B %Y %H:%M UTC")



    if settings["use_cache"]:
        configs = load_data("cache_configs.json")
        data = load_data("cache_data.json")
    else:
        configs =  config_manager.get_configs()
        analyzer = Analyzer(configs, settings)
        data = analyzer.analyze_things()

        if settings["dev_buildroot"]:
            dump_data("cache_configs.json", configs)
            dump_data("cache_data.json", data)



    # measuring time of execution
    time_analysis_time = datetime_now_string()


    # -------------------------------------------------
    # Stage 2: Generating pages and data outputs
    # -------------------------------------------------

    query = Query(data, configs, settings)

    generate_pages(query)
    generate_data_files(query)
    generate_historic_data(query)


    # -------------------------------------------------
    # Done! Printing final summary
    # -------------------------------------------------

    # measuring time of execution
    time_ended = datetime_now_string()

    # Print extra metrics
    if not settings["use_cache"]:
        analyzer.print_metrics()

    # Print base metrics
    log("")
    log("=============================")
    log("Feedback Pipeline build done!")
    log("=============================")
    log("")
    log("  Started:       {}".format(time_started))
    log("  Analysis done: {}".format(time_analysis_time))
    log("  Finished:      {}".format(time_ended))
    log("")



if __name__ == "__main__":
    main()
