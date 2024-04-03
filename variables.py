import configparser
import json
import os
from os import path, environ


class Variables:

    def __init__(self, input_file_name):
        self.var = dict()
        dir_path = os.path.dirname(os.path.realpath(__file__))
        input_file_name = os.path.join(dir_path, 'config', input_file_name)
        self.set_variables_from_cfg(input_file_name)

    def set_variables_from_json(self, input_file_name):
        with open(input_file_name) as f:
            self.var = json.load(f)

    def set_variables_from_cfg(self, input_file_name):
        cfg = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
        cfg.optionxform = lambda option: option  # override default conversion of key to lowercase

        with open(input_file_name) as cfg_file:
            # cfg.read_file(cfg_file)
            cfg_txt = os.path.expandvars(cfg_file.read())
            cfg.read_string(cfg_txt)
            default = cfg['DEFAULT']
            for key, value in default.items():
                self.var[key] = value

    def get(self, variable_name):
        if self.exists(variable_name):
            return self.var[variable_name]
        else:
            return None

    def set(self, variable_name, variable_value):
        self.var[variable_name] = variable_value

    def exists(self, variable_name):
        if variable_name in self.var.keys():
            return 1
        else:
            return 0

    def replace_variable_with_value(self, input_string: str, local_var: dict = None):
        if local_var is not None:
            for key, value in local_var.items():
                input_string = input_string.replace("${" + key + "}", str(value))

        for key in self.var.keys():
            input_string = input_string.replace("${" + key + "}", str(self.var[key]))

        return input_string
