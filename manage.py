#! /usr/bin/env python

import os
import signal
import subprocess
import warnings

from flask.exthook import ExtDeprecationWarning
from flask_script import Manager
from os import getenv
from sqlalchemy.exc import IntegrityError, ProgrammingError

from plenario import create_app as server
from plenario.settings import DATABASE_CONN, REDSHIFT_CONN, DB_NAME
from plenario.settings import DEFAULT_USER
from plenario.worker import create_worker as worker


# Ignore warnings stating that libraries we depend on use deprecated flask code
warnings.filterwarnings("ignore", category=ExtDeprecationWarning)
# Ignore warnings stating that our forms do not address all model fields
warnings.filterwarnings("ignore", "Fields missing from ruleset", UserWarning)


apps = {
    "server": server,
    "worker": worker
}

application = apps["worker"]() if getenv("WORKER", None) else apps["server"]()
manager = Manager(application)


@manager.command
def runserver():
    """Start up plenario server."""

    application.run(host="0.0.0.0", port=5000)


@manager.command
def worker():
    """Start up celery worker."""

    celery_commands = ["celery", "-A", "plenario.tasks", "worker", "-l", "INFO"]
    wait(subprocess.Popen(celery_commands))


@manager.command
def monitor():
    """Start up flower task monitor."""

    flower_commands = ["flower", "-A", "plenario.tasks", "--persistent"]
    wait(subprocess.Popen(flower_commands))


@manager.command
def pg():
    """Psql into postgres."""

    print("[plenario] Connecting to %s" % DATABASE_CONN)
    wait(subprocess.Popen(["psql", DATABASE_CONN]))


@manager.command
def rs():
    """Psql into redshift."""

    print("[plenario] Connecting to %s" % REDSHIFT_CONN)
    wait(subprocess.Popen(["psql", REDSHIFT_CONN]))


@manager.command
def test():
    """Run nosetests."""

    nose_commands = ["nosetests", "-s", "tests", "-vv"]
    wait(subprocess.Popen(nose_commands))


@manager.command
def config():
    """Set up environment variables for plenario."""

    pass


@manager.command
def init():
    """Initialize the database."""

    from plenario.database import create_database
    from sqlalchemy import create_engine

    base_uri = DATABASE_CONN.rsplit('/', 1)[0]
    base_engine = create_engine(base_uri)

    try:
        create_database(base_engine, DB_NAME)
    except ProgrammingError:
        print('[plenario] It already exists!')

    from plenario.database import create_extension
    from plenario.database import app_engine as plenario_engine, Base
    from plenario.utils.weather import WeatherETL, WeatherStationsETL

    for extension in ['plv8', 'postgis']:
        try:
            create_extension(plenario_engine, extension)
        except ProgrammingError:
            print('[plenario] It already exists!')

    print('[plenario] Creating metadata tables')
    Base.metadata.create_all()

    print('[plenario] Creating weather tables')
    WeatherETL().make_tables()
    WeatherStationsETL().make_station_table()

    from plenario.database import psql

    # Set up custom functions, triggers and views in postgres
    psql("./plenario/dbscripts/audit_trigger.sql")
    psql("./plenario/dbscripts/point_from_location.sql")
    psql("./plenario/dbscripts/sensors_trigger.sql")

    # Set up the default user if we are running in anything but production
    if os.environ.get('CONFIG') != 'prod':
        from plenario.database import session
        from plenario.models.User import User

        print('[plenario] Create default user')
        user = User(**DEFAULT_USER)

        try:
            session.add(user)
            session.commit()
        except IntegrityError:
            print('[plenario] Already exists!')
            session.rollback()

    from plenario.tasks import health

    # This will get celery to set up its meta tables
    health.delay()


def wait(process):
    """Waits on a process and passes along sigterm."""

    try:
        signal.pause()
    except (KeyboardInterrupt, SystemExit):
        process.terminate()
        process.wait()


if __name__ == "__main__":
    manager.run()
