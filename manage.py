#!/usr/bin/env python

import logging
import os
import signal
import subprocess
import warnings
from os import getenv
from time import sleep

import sqlalchemy.exc
from flask.exthook import ExtDeprecationWarning
from flask_script import Manager
from kombu.exceptions import OperationalError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, ProgrammingError

from plenario.database import create_database, create_extension, postgres_base, psql, postgres_session, \
        drop_database, postgres_engine as plenario_engine
from plenario.models.User import User
from plenario.server import create_app as server
from plenario.settings import DATABASE_CONN, REDSHIFT_CONN, DB_NAME, DEFAULT_USER
from plenario.tasks import health
from plenario.utils.weather import WeatherETL, WeatherStationsETL
from plenario.worker import create_worker as worker


logger = logging.getLogger('manage.py')
logger.setLevel(logging.DEBUG)


# Ignore warnings stating that libraries we depend on use deprecated flask code
warnings.filterwarnings('ignore', category=ExtDeprecationWarning)
# Ignore warnings stating that our forms do not address all model fields
warnings.filterwarnings('ignore', 'Fields missing from ruleset', UserWarning)


apps = {
    'server': server,
    'worker': worker
}

if getenv('WORKER', None):
    application = apps['worker']()
else:
    application = apps['server']()
manager = Manager(application)


@manager.command
def runserver():
    """Start up plenario server.
    """
    application.run(host='0.0.0.0', port=5000, debug=os.environ.get('DEBUG'))


@manager.command
def worker():
    """Start up celery worker.
    """
    celery_commands = ['celery', '-A', 'plenario.tasks', 'worker', '-l', 'INFO']
    wait(subprocess.Popen(celery_commands))


@manager.command
def monitor():
    """Start up flower task monitor.
    """
    flower_commands = ['flower', '-A', 'plenario.tasks', '--persistent']
    wait(subprocess.Popen(flower_commands))


@manager.command
def pg():
    """Psql into postgres.
    """
    logger.debug('[plenario] Connecting to %s' % DATABASE_CONN)
    wait(subprocess.Popen(['psql', DATABASE_CONN]))


@manager.command
def rs():
    """Psql into redshift.
    """
    logger.debug('[plenario] Connecting to %s' % REDSHIFT_CONN)
    wait(subprocess.Popen(['psql', REDSHIFT_CONN]))


@manager.command
def test():
    """Run nosetests.
    """
    test_cmds = [
        ['nosetests', '--nologcapture', 'tests/test_api/test_point.py', '-v'],
        ['nosetests', '--nologcapture', 'tests/test_api/test_shape.py', '-v'],
        ['nosetests', '--nologcapture', 'tests/test_api/test_validator.py', '-v'],
        ['nosetests', '--nologcapture', 'tests/test_etl/test_point.py', '-v'],
        ['nosetests', '--nologcapture', 'tests/submission/', '-v'],
        ['nosetests', '--nologcapture', 'tests/test_sensor_network/test_sensor_networks.py', '-v'],
        ['nosetests', '--nologcapture', 'tests/test_models/test_feature_meta.py', '-v'],
        ['nosetests', '--nologcapture', 'tests/test_sensor_network/test_nearest.py', '-v'],
    ]
    for cmd in test_cmds:
        wait(subprocess.Popen(cmd))


# @manager.command
# def config():
#     """Set up environment variables for plenario."""
#     pass


@manager.command
def init():
    """Initialize the database.
    """
    # TODO(heyzoos)
    # Check for dependencies to fail fast and helpfully before running:
    #   - postgresql-client

    base_uri = DATABASE_CONN.rsplit('/', 1)[0]
    base_engine = create_engine(base_uri)

    connection_attempts = 6
    interval = 10
    for connection_attempt in range(0, connection_attempts):
        try:
            create_database(base_engine, DB_NAME)
            break
        except ProgrammingError:
            logger.debug('[plenario] It already exists!')
            break
        except sqlalchemy.exc.OperationalError:
            logger.debug('[plenario] Database has not started yet.')
            sleep(interval)

    try:
        create_extension(plenario_engine, 'postgis')
        create_extension(plenario_engine, 'plv8')
    except ProgrammingError:
        logger.debug('[plenario] It already exists!')

    logger.debug('[plenario] Creating metadata tables')
    postgres_base.metadata.create_all()

    logger.debug('[plenario] Creating weather tables')
    WeatherStationsETL().make_station_table()
    WeatherETL().make_tables()

    # Set up custom functions, triggers and views in postgres
    psql('./plenario/dbscripts/sensor_tree.sql')
    psql('./plenario/dbscripts/point_from_location.sql')

    # Set up the default user if we are running in anything but production
    if os.environ.get('CONFIG') != 'prod':
        logger.debug('[plenario] Create default user')
        user = User(**DEFAULT_USER)

        try:
            postgres_session.add(user)
            postgres_session.commit()
        except IntegrityError:
            logger.debug('[plenario] Already exists!')
            postgres_session.rollback()

    # This will get celery to set up its meta tables
    try:
        health.delay()
    except OperationalError:
        logger.debug('[plenario] Redis is not running!')


@manager.command
def uninstall():
    """Drop the plenario databases.
    """
    base_uri = DATABASE_CONN.rsplit('/', 1)[0]
    base_engine = create_engine(base_uri)
    try:
        drop_database(base_engine, 'plenario_test')
    except ProgrammingError:
        pass

    base_uri = REDSHIFT_CONN.rsplit('/', 1)[0]
    base_engine = create_engine(base_uri)
    try:
        drop_database(base_engine, 'plenario_test')
    except ProgrammingError:
        pass


def wait(process):
    """Waits on a process and passes along sigterm.
    """
    try:
        signal.pause()
    except (KeyboardInterrupt, SystemExit):
        process.terminate()
        process.wait()


if __name__ == '__main__':
    manager.run()
