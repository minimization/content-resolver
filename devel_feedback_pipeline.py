#!/usr/bin/python3

from feedback_pipeline import *
import argparse

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("output", help="Directory to contain the output.")
    args = parser.parse_args()

    log("Loading devel data...")
    configs = load_data("devel_configs.json")
    installs = load_data("devel_data.json")
    historic_data = load_data("devel_historic_data.json")
 
    data = get_data(configs, installs)

    get_historic_chart_data(historic_data, data)

    generate_pages(data, args.output)
    generate_reports_by_base(data, args.output)
    generate_reports_by_use_case(data, args.output)
    generate_reports_bases_releases(data, args.output)
    generate_reports_bases_definitions(data, args.output)
    generate_reports_use_cases_definitions(data, args.output)

if __name__ == "__main__":
    main()
