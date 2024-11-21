import datetime
import json
import re
import sys
import jinja2

class SetEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, jinja2.Environment):
            return ""
        return json.JSONEncoder.default(self, obj)



def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)
    return data

def log(msg):
    print(msg, file=sys.stderr)


def err_log(msg):
    print("ERROR LOG:  {}".format(msg), file=sys.stderr)

def pkg_id_to_name(pkg_id):
    pkg_name = pkg_id.rsplit("-",2)[0]
    return pkg_name


def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file, cls=SetEncoder)


def size(num, suffix='B'):
    for unit in ['','k','M','G']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'T', suffix)


def workload_id_to_conf_id(workload_id):
    workload_conf_id = workload_id.split(":")[0]
    return workload_conf_id

def url_to_id(url):

    # strip the protocol
    if url.startswith("https://"):
        url = url[8:]
    elif url.startswith("http://"):
        url = url[7:]

    # strip a potential leading /
    if url.endswith("/"):
        url = url[:-1]

    # and replace all non-alphanumeric characters with -
    regex = re.compile('[^0-9a-zA-Z]')
    return regex.sub("-", url)


def datetime_now_string():
    return datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")