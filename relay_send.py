#!/usr/bin/env python
"""Run with the SAME Python interpreter as the existing relay daemon.

Compatible with Python 2.7 and Python 3. Never accepts a caller-selected command.
The bridge bounds this helper's execution time externally.
"""
from multiprocessing.connection import Client
import json
import pickle
import sys


def main():
    with open(sys.argv[1]) as stream:
        config = json.load(stream)
    connection = Client(('localhost', int(config['relay_port'])),
                        authkey=config['process_passwd'].encode('utf-8'))
    try:
        connection.send_bytes(pickle.dumps('open_door', protocol=2))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
