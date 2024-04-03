import logging
import os
from datetime import datetime
from variables import Variables

def setup_logging():
    """
    Set up logging configuration.

    """
    vars = Variables('ENV.cfg')

    curr_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    log_file = os.path.join(vars.get('LOG_DIR'),f'sf_worksheet_{curr_time}.log')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(asctime)s : %(message)s', filename=log_file)

    # Create a console handler and set its level to INFO
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Create a formatter and set the format for the console handler
    formatter = logging.Formatter('%(levelname)s %(asctime)s: %(message)s')
    console_handler.setFormatter(formatter)

    # Add the console handler to the root logger
    logging.getLogger().addHandler(console_handler)
    formatter = logging.Formatter('%(levelname)s %(asctime)s : %(message)s')
