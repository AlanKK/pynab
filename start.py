import argparse
import concurrent.futures
import time
import pytz
import datetime
import traceback
import psycopg2.extensions

from pynab import log, log_descriptor
from pynab.db import db_session, Group, Binary, engine

import pynab.groups
import pynab.binaries
import pynab.releases
import pynab.tvrage
import pynab.rars
import pynab.nfos
import pynab.imdb
import config


def update(group_name):
    try:
        return pynab.groups.update(group_name)
    except Exception as e:
        log.critical(traceback.format_exc())
        raise Exception


def daemonize(pidfile):
    try:
        import traceback
        from daemonize import Daemonize

        fds = []
        if log_descriptor:
            fds = [log_descriptor]

        daemon = Daemonize(app='pynab', pid=pidfile, action=main, keep_fds=fds)
        daemon.start()
    except SystemExit:
        raise
    except:
        log.critical(traceback.format_exc())


def main():
    log.info('start: starting update...')

    while True:
        # refresh the db session each iteration, just in case
        with db_session() as db:
            active_groups = [group.name for group in db.query(Group).filter(Group.active==True).all()]
            if active_groups:
                with concurrent.futures.ThreadPoolExecutor(config.scan.get('update_threads', None)) as executor:
                    # if maxtasksperchild is more than 1, everything breaks
                    # they're long processes usually, so no problem having one task per child
                    result = [executor.submit(update, active_group) for active_group in active_groups]
                    #result = executor.map(update, active_groups)
                    for r in concurrent.futures.as_completed(result):
                        data = r.result()

                # process binaries
                log.info('start: processing binaries...')
                pynab.binaries.process()

                # process releases
                log.info('start: processing releases...')
                pynab.releases.process()

                # clean up dead binaries
                if config.scan.get('dead_binary_age', 3) != 0:
                    dead_time = pytz.utc.localize(datetime.datetime.now()) - datetime.timedelta(days=config.scan.get('dead_binary_age', 3))
                    dead_binaries = db.query(Binary).filter(Binary.posted<=dead_time).delete()
                    log.info('start: deleted {} dead binaries'.format(dead_binaries))
            else:
                log.info('start: no groups active, cancelling start.py...')
                break

            # vacuum the segments, parts and binaries tables
            log.info('start: vacuuming relevant tables...')
            conn = engine.connect()
            conn.connection.connection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            conn.execute('VACUUM binaries')
            conn.execute('VACUUM parts')
            conn.execute('VACUUM segments')
            conn.close()

        # wait for the configured amount of time between cycles
        update_wait = config.scan.get('update_wait', 300)
        log.info('start: sleeping for {:d} seconds...'.format(update_wait))
        time.sleep(update_wait)


if __name__ == '__main__':
    argparser = argparse.ArgumentParser(description="Pynab main indexer script")
    argparser.add_argument('-d', '--daemonize', action='store_true', help='run as a daemon')
    argparser.add_argument('-p', '--pid-file', help='pid file (when -d)')

    args = argparser.parse_args()

    if args.daemonize:
        pidfile = args.pid_file or config.scan.get('pid_file')
        if not pidfile:
            log.error("A pid file is required to run as a daemon, please supply one either in the config file '{}' or as argument".format(config.__file__))
        else:
            daemonize(pidfile)
    else:
        main()
