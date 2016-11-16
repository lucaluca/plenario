import calendar
import dateutil.parser
import metar.metar as metar
import numpy
import requests
import pandas
import odo
import operator
import os
import re
import sys
import tarfile
import zipfile

import plenario.utils.weather_metar as WeatherMetar

from csvkit.unicsv import UnicodeCSVReader, UnicodeCSVWriter,FieldSizeLimitError
from datetime import datetime, date, timedelta
from dateutil import relativedelta
from ftplib import FTP
from geoalchemy2 import Geometry
from io import StringIO
from pandas import to_numeric
from slugify import slugify
from sqlalchemy import BigInteger, and_, select, distinct, func, delete, join
from sqlalchemy import Table, Column, String, Date, DateTime, Integer, Float
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import ARRAY, FLOAT, VARCHAR
from tempfile import NamedTemporaryFile

from plenario.database import session as session, app_engine as engine, Base
from plenario.settings import DATA_DIR, DATABASE_CONN
from plenario.utils.helpers import reflect


def get_cardinal_direction(wind_direction: str) -> str:
    """Given a wind direction, convert it to its cardinal direction
    equivalent. Based on: http://stackoverflow.com/questions/7490660"""

    if wind_direction.strip() in ['VR', 'M', 'VRB']:
        return "VRB"

    try:
        wind_direction = float(wind_direction)
    except ValueError:
        return None

    val = int((wind_direction / 22.5) + 0.5)
    cardinal_directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return cardinal_directions[(val % 16)]


def snake_case(name: str) -> str:
    """Helper function to convert camelcased strings to snake_case. Copied
    from this snippet: http://stackoverflow.com/questions/1175208."""

    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def get_callsign_wban_code_map() -> dict:
    """Generate map of callsigns to wban codes from weather_stations table."""

    stations = reflect("weather_stations", Base.metadata, engine)
    selection = select([stations.c.call_sign, stations.c.wban_code])
    return dict(engine.execute(selection).fetchall())


def parse_metar(raw_metar: str) -> metar.Metar:
    """Attempt to convert a string to a metar object. Return nothing on fail."""

    try:
        return metar.Metar(raw_metar)
    except metar.ParserError:
        return None


def extract_hourly_values(series: pandas.Series) -> pandas.Series:
    """Given a single row of data, perform all the hourly transformations
    required and return the transformed row."""

    return pandas.Series({
        "wban_code": series.wban,
        "datetime": dateutil.parser.parse(series.date + series.time),
        "old_station_type": None,
        "station_type": to_numeric(series.station_type, errors="coerce"),
        "sky_condition": series.sky_condition,
        "sky_condition_top": series.sky_condition.split(" ")[-1],
        "visibility": to_numeric(series.visibility, errors="coerce"),
        "weather_types": series.weather_type,
        "drybulb_fahrenheit": to_numeric(series.dry_bulb_farenheit),
        "wetbulb_fahrenheit": to_numeric(series.wet_bulb_farenheit),
        "dewpoint_fahrenheit": to_numeric(series.dew_point_farenheit),
        "relative_humidity": to_numeric(series.relative_humidity),
        "wind_speed": to_numeric(series.wind_speed),
        "wind_direction": series.wind_direction,
        "wind_direction_cardinal": get_cardinal_direction(series.wind_direction),
        "station_pressure": to_numeric(series.station_pressure),
        "sealevel_pressure": to_numeric(series.sea_level_pressure),
        "report_type": series.record_type,
        "hourly_precip": to_numeric(series.hourly_precip)
    })


def extract_metar_values(df: pandas.DataFrame) -> pandas.DataFrame:
    """Generate a series of Metar objects from the python-metar library and use
    them to create a new dataframe."""

    # Covert the raw text column into a series of Metar objects, drop all the
    # heathens that failed to convert
    metars = df["raw_text"].apply(parse_metar).dropna()

    # Extract and transform all the data using methods on the Metar objects
    df = pandas.DataFrame()
    # todo: this is terrible, we should only go through the dataframe once...
    df["call_sign"] = metars.apply(lambda row: row.station_id)
    df["datetime"] = metars.apply(lambda row: row.time)
    df["sky_condition"] = metars.apply(lambda row: WeatherMetar.getSkyCondition(row)[0])
    df["sky_condition_top"] = metars.apply(lambda row: WeatherMetar.getSkyCondition(row)[1])
    df["visibility"] = metars.apply(WeatherMetar.getVisibility)
    df["weather_types"] = metars.apply(lambda row: WeatherMetar.getWeatherTypes(row))
    df["temp_fahrenheit"] = metars.apply(WeatherMetar.getTempFahrenheit)
    df["dewpoint_fahrenheit"] = metars.apply(WeatherMetar.getDewpointFahrenheit)
    df["wind_speed"] = metars.apply(lambda row: WeatherMetar.getWind(row)[0])
    df["wind_direction"] = metars.apply(lambda row: WeatherMetar.getWind(row)[1])
    df["wind_direction_cardinal"] = metars.apply(lambda row: WeatherMetar.getWind(row)[2])
    df["wind_gust"] = metars.apply(lambda row: WeatherMetar.getWind(row)[3])
    df["station_pressure"] = metars.apply(WeatherMetar.getPressure)
    df["sealevel_pressure"] = metars.apply(WeatherMetar.getPressureSeaLevel)
    df["precip_1h"] = metars.apply(lambda row: WeatherMetar.getPrecip(row)[0])
    df["precip_3h"] = metars.apply(lambda row: WeatherMetar.getPrecip(row)[1])
    df["precip_6h"] = metars.apply(lambda row: WeatherMetar.getPrecip(row)[2])
    df["precip_24h"] = metars.apply(lambda row: WeatherMetar.getPrecip(row)[3])

    # Derive wban codes from station callsigns
    callsign_to_wban_map = get_callsign_wban_code_map()
    df["wban_code"] = metars.apply(lambda row: callsign_to_wban_map.get(row.station_id))
    df.dropna(subset=["wban_code"], inplace=True)

    return df


def insert_new_weather_observations(temp: Table, target: Table) -> None:
    """Given two weather tables, insert the rows from the temp table which are
    not found in the target table."""

    # The sorted calls in this and the next query ensure that the order of
    # the columns in the select and insert match up. Messing up the
    # ordering causes an error, ex: insert into (x, y, z) (select x, z, y ... )
    select_new_records = select(
        sorted(temp.columns, key=lambda col: col.name)
    ).select_from(join(
        left=temp,
        right=target,
        onclause=and_(
            temp.c.datetime == target.c.datetime,
            temp.c.wban_code == target.c.wban_code
        ),
        isouter=True
    )).where(target.c.datetime == None)

    insert_new_records = target.insert().from_select(
        names=sorted(target.columns, key=lambda col: col.name),
        select=select_new_records
    )

    engine.execute(insert_new_records)


# Used to convert column types from the pandas generated metar table to types
# which are compatible with the existing table
metar_dtypes = {
    "precip_3h": FLOAT(),
    "precip_6h": FLOAT(),
    "precip_24h": FLOAT(),
    "weather_types": ARRAY(VARCHAR)
}


def update_metar() -> None:
    """Update the dat_weather_metar table with recently reported and unchecked
    NOAA weather observations. This method is meant to be idempotent."""

    # Extract the metar cache csv into a pandas dataframe
    metar_cache_response = requests.get(
        "http://aviationweather.gov/adds/dataserver_current/current/"
        "metars.cache.csv"
    )
    metar_csv = NamedTemporaryFile()
    # Skip the 5 header lines that come with the csv to not break the parser
    metar_csv.write(metar_cache_response.content.split(b"\n", 5)[-1])
    metars = pandas.read_csv(
        metar_csv.name,
        delimiter=",",
        parse_dates=["observation_time"]
    )
    metar_csv.close()

    # Derive values from the raw text column into separate columns
    metars = extract_metar_values(metars)

    # To the database damn you
    metars.to_sql(
        "tmp_weather_observations_metar", engine,
        if_exists="replace",
        dtype=metar_dtypes
    )

    tmp_table = reflect("tmp_weather_observations_metar", Base.metadata, engine)
    dat_table = reflect("dat_weather_observations_metar", Base.metadata, engine)
    insert_new_weather_observations(tmp_table, dat_table)


def update_weather(timespans: list) -> None:
    """Update the dat_weather_observations_<hourly/daily/monthly> table with
    quality-checked NOAA weather observations."""

    postgres_url = DATABASE_CONN

    # Enables update weather to take strings also
    timespans = [timespans] if not isinstance(timespans, list) else timespans

    # # Get the source file containing all the weather observation data
    current_year_month = datetime.now().strftime("%Y%m")
    source_url = "http://www.ncdc.noaa.gov/orders/qclcd/"
    source_url += "QCLCD{}.zip".format(current_year_month)
    response = requests.get(source_url)

    # Write it into a temporary zip file
    temporary_zip = NamedTemporaryFile()
    temporary_zip.file.write(response.content)

    # Generate a zip object that will let us pull individual files
    numpy_zip = numpy.load(temporary_zip.name)

    for timespan in timespans:

        tmp_table_name = postgres_url + "::tmp_weather_observations_" + timespan
        dat_table_name = postgres_url + "::dat_weather_observations_" + timespan

        # These methods assume that the tmp and dat tables exist
        tmp_table = odo.resource(tmp_table_name)
        dat_table = odo.resource(dat_table_name)

        # Pull the file out of the zip, and load it into a temporary csv
        data_fname = "{}.txt".format(current_year_month + timespan)
        staging_csv = NamedTemporaryFile(suffix=".csv")
        staging_csv.file.write(numpy_zip[data_fname])

        # Returns an iterator used to break the csv up into manageable portions
        dframe_chunks = pandas.read_csv(
            staging_csv.name,
            chunksize=100000,
            dtype=str
        )

        # Clean out the temporary table
        engine.execute(delete(tmp_table))

        # Munge and load each chunk into a temporary table
        for df in dframe_chunks:
            df = df.rename(columns=snake_case)
            # todo: apply is slowest part of the code... (20m for 1.6mil rows)
            df = df.apply(extract_hourly_values, axis=1)
            dshape = odo.discover(df)
            odo.odo(df, tmp_table_name, dshape=dshape)

        insert_new_weather_observations(tmp_table, dat_table)

        staging_csv.close()
    temporary_zip.close()


def update_stations() -> None:
    """Update the weather station listing in the weather_stations table.
    Because the size of the source file is small (~3mb) and the number of
    raw rows is small also (~30k), drop and replace the table each time the
    method is run."""

    # Read the source data into a temporary csv file
    stations_csv = NamedTemporaryFile(mode="w")
    ftp_client = FTP('ftp.ncdc.noaa.gov')
    ftp_client.login()
    ftp_client.retrlines(
        cmd="RETR /pub/data/noaa/isd-history.csv",
        # Custom callback avoids the random EOF I was encountering
        callback=lambda line: stations_csv.file.write(line + "\n")
    )
    ftp_client.close()

    # Create a dataframe from the csv, specifying datetime columns but letting
    # pandas infer the types of the rest
    stations = pandas.read_csv(
        stations_csv.name,
        delimiter=",",
        parse_dates=["BEGIN", "END"]
    )
    stations_csv.close()

    # Slugify column names and rename some to play nice with legacy code
    stations = stations.rename(columns=lambda col: slugify(col, separator="_"))
    stations = stations.rename(
        columns={
            "wban": "wban_code",
            "ctry": "country",
            "icao": "call_sign",
            "elev_m": "elevation"
        }
    )

    # Munge the data, dropping rows with missing values, duplicate and
    # undesirable wbans, and funky location values
    stations = stations.dropna(how="any")
    stations = stations.drop_duplicates("wban_code")
    stations = stations[stations["wban_code"] != 99999]
    stations = stations[(stations["lon"] != 0) & (stations["lat"] != 0)]
    stations.reset_index(drop=True, inplace=True)

    # Condense the lat long columns to a location column containing a geometry
    stations["location"] = stations[["lon", "lat"]].apply(
        func=lambda row: "SRID=4326;POINT(%s %s)" % (row.lon, row.lat),
        axis=1
    )
    del stations["lon"]
    del stations["lat"]

    # Insert the dataframe values into postgres, replacing the existing table
    stations.to_sql(
        "weather_stations", engine,
        if_exists="replace",
        dtype={"location": Geometry("point", 4326)}
    )


# ==============================================================================
# ==============================================================================
# ================================ THE WALL ====================================
# ==============================================================================
# ==============================================================================

class WeatherETL(object):
    """ 
    Download, transform and insert weather data into plenario
    """

    # contents:
    # - initialize() functions (initialize(), initialize_month(), metar_initialize_current())
    # - _do_etl() (_do_etl(), _metar_do_etl())
    # - _cleanup_temp_tables, _metar_cleanup_temp_tables
    # - _add_location() (not called?)
    # - _update(), _update_metar():
    #      - Idea here is to create a new new_table which will represent the intersection (?) of src and dat -- only new records
    #      - We are eventually storing in dat_table
    #      - Raw incoming data is in src_table
    # - make_tables(), metar_make_tables()
    # - _extract(fname)
    #
    #

    # todo: not used???
    # weather_type_dict = {'+FC': 'TORNADO/WATERSPOUT',
    #                  'FC': 'FUNNEL CLOUD',
    #                  'TS': 'THUNDERSTORM',
    #                  'GR': 'HAIL',
    #                  'RA': 'RAIN',
    #                  'DZ': 'DRIZZLE',
    #                  'SN': 'SNOW',
    #                  'SG': 'SNOW GRAINS',
    #                  'GS': 'SMALL HAIL &/OR SNOW PELLETS',
    #                  'PL': 'ICE PELLETS',
    #                  'IC': 'ICE CRYSTALS',
    #                  'FG': 'FOG', # 'FG+': 'HEAVY FOG (FG & LE.25 MILES VISIBILITY)',
    #                  'BR': 'MIST',
    #                  'UP': 'UNKNOWN PRECIPITATION',
    #                  'HZ': 'HAZE',
    #                  'FU': 'SMOKE',
    #                  'VA': 'VOLCANIC ASH',
    #                  'DU': 'WIDESPREAD DUST',
    #                  'DS': 'DUSTSTORM',
    #                  'PO': 'SAND/DUST WHIRLS',
    #                  'SA': 'SAND',
    #                  'SS': 'SANDSTORM',
    #                  'PY': 'SPRAY',
    #                  'SQ': 'SQUALL',
    #                  'DR': 'LOW DRIFTING',
    #                  'SH': 'SHOWER',
    #                  'FZ': 'FREEZING',
    #                  'MI': 'SHALLOW',
    #                  'PR': 'PARTIAL',
    #                  'BC': 'PATCHES',
    #                  'BL': 'BLOWING',
    #                  'VC': 'VICINITY'
    #                  # Prefixes:
    #                  # - LIGHT
    #                  # + HEAVY
    #                  # "NO SIGN" MODERATE
    #              }

    current_row = None

    def __init__(self, data_dir=DATA_DIR, debug=False):
        self.base_url = 'http://www.ncdc.noaa.gov/orders/qclcd'
        self.data_dir = data_dir
        self.debug_outfile = sys.stdout
        self.debug = debug
        self.out_header = None
        if (self.debug == True):
            self.debug_filename = os.path.join(self.data_dir, 'weather_etl_debug_out.txt')
            sys.stderr.write( "writing out debug_file %s\n" % self.debug_filename)
            self.debug_outfile = open(self.debug_filename, 'w+')

    def initialize_month(self, year, month, no_daily=False, no_hourly=False, weather_stations_list = None, banned_weather_stations_list = None, start_line=0, end_line=None):
        self.make_tables()
        fname = self._extract_fname(year,month)
        self._do_etl(fname, no_daily, no_hourly, weather_stations_list, banned_weather_stations_list, start_line, end_line)

    ######################################################################
    # do_etl: perform the ETL on a given tar/zip file
    #    weather_stations_list: a list of WBAN codes (as strings) in order to only read a subset of station observations
    #    start_line, end_line: optional parameters for testing which will only read a subset of lines from the file
    ######################################################################
    def _do_etl(self, fname, no_daily=False, no_hourly=False, weather_stations_list=None, banned_weather_stations_list = None, start_line=0, end_line=None):

        raw_hourly, raw_daily, file_type = self._extract(fname)

        if (self.debug):
            self.debug_outfile.write("Extracting: %s\n" % fname)
        
        if (not no_daily):
            t_daily = self._transform_daily(raw_daily, file_type, 
                                            weather_stations_list=weather_stations_list,
                                            banned_weather_stations_list=banned_weather_stations_list,
                                            start_line=start_line, end_line=end_line)
        if (not no_hourly):
            t_hourly = self._transform_hourly(raw_hourly, file_type, 
                                              weather_stations_list=weather_stations_list, 
                                              banned_weather_stations_list=banned_weather_stations_list,
                                              start_line=start_line, end_line=end_line)
        if (not no_daily):
            self._load_daily(t_daily)                          # this actually imports the transformed StringIO csv
            self._update(span='daily')
            # self._add_location(span='daily') # XXX mcc: hmm
        if (not no_hourly):
            self._load_hourly(t_hourly)    # this actually imports the transformed StringIO csv
            self._update(span='hourly')
            # self._add_location(span='hourly') # XXX mcc: hmm
        #self._cleanup_temp_tables()

    def _cleanup_temp_tables(self):
        for span in ['daily', 'hourly']:
            for tname in ['src', 'new']:
                try:
                    table = getattr(self, '%s_%s_table' % (tname, span)) # possibly not getting dropped
                    table.drop(engine, checkfirst=True)
                except AttributeError:
                    continue

    # todo: covered by insert_new_weather_observations
    # def _update(self, span=None):
    #     new_table = Table('new_weather_observations_%s' % span, Base.metadata,
    #                       Column('wban_code', String(5)), keep_existing=True)
    #     dat_table = getattr(self, '%s_table' % span)
    #     src_table = getattr(self, 'src_%s_table' % span)
    #     from_sel_cols = ['wban_code']
    #     if span == 'daily':
    #         from_sel_cols.append('date')
    #         src_date_col = src_table.c.date
    #         dat_date_col = dat_table.c.date
    #         new_table.append_column(Column('date', Date))
    #         new_date_col = new_table.c.date
    #     elif span == 'hourly':
    #         from_sel_cols.append('datetime')
    #         src_date_col = src_table.c.datetime
    #         dat_date_col = dat_table.c.datetime
    #         new_table.append_column(Column('datetime', DateTime))
    #         new_date_col = new_table.c.datetime
    #     new_table.drop(engine, checkfirst=True)
    #     new_table.create(engine)
    #     ins = new_table.insert()\
    #             .from_select(from_sel_cols,
    #                 select([src_table.c.wban_code, src_date_col])\
    #                     .select_from(src_table.join(dat_table,
    #                         and_(src_table.c.wban_code == dat_table.c.wban_code,
    #                              src_date_col == dat_date_col),
    #                         isouter=True)
    #                 ).where(dat_table.c.id == None)
    #             )
    #     #print "_update: span=%s: sql is'%s'" % (span, ins)
    #     conn = engine.contextual_connect()
    #     try:
    #         conn.execute(ins)
    #         new = True
    #     except TypeError:
    #         new = False
    #     if new:
    #         ins = dat_table.insert()\
    #                 .from_select([c for c in dat_table.columns if c.name != 'id'],
    #                     select([c for c in src_table.columns])\
    #                         .select_from(src_table.join(new_table,
    #                             and_(src_table.c.wban_code == new_table.c.wban_code,
    #                                  src_date_col == new_date_col)
    #                         ))
    #                 )
    #         #print "_update NEW : span=%s: sql is'%s'" % (span, ins)
    #         conn.execute(ins)

    def make_tables(self):
        self._make_daily_table()
        self._make_hourly_table()

    def metar_make_tables(self):
        self._make_metar_table()
            
    ########################################
    ########################################
    # Extract (from filename / URL to some raw StringIO()
    ########################################
    ########################################
    def _download_write(self, fname):
        fpath = os.path.join(self.data_dir, fname)
        url = '%s/%s' % (self.base_url, fname)
        if (self.debug==True):
            self.debug_outfile.write("Extracting: %s\n" % url)
        r = requests.get(url, stream=True)
        with open(fpath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
                    f.flush()
        f.close() # Explicitly close before re-opening to read.

    def _extract(self, fname):
        file_type = 'zipfile'

        if fname.endswith('.zip'):
            file_type = 'zipfile'
        elif fname.endswith('tar.gz'):
            file_type = 'tarfile'
        else:
            print(("file type for ", fname, "not found: quitting"))
            return None

        # extract the year and month from the QCLCD filename
        fname_spl = fname.split('.')
        # look at the 2nd to last string
        fname_yearmonth = (fname_spl[:-1])[0]
        yearmonth_str = fname_yearmonth[-6:]
        year_str = yearmonth_str[0:4]
        month_str = yearmonth_str[4:6]
        
        fpath = os.path.join(self.data_dir, fname)
        raw_weather_hourly = StringIO()
        raw_weather_daily = StringIO()
        
        now_month, now_year = str(datetime.now().month), str(datetime.now().year)
        if '%s%s' % (now_year.zfill(2), now_month.zfill(2)) == yearmonth_str:
            self._download_write(fname)

        elif not os.path.exists(fpath):
            self._download_write(fname)

        if file_type == 'tarfile':
            with tarfile.open(fpath, 'r') as tar:
                for tarinfo in tar:
                    if tarinfo.name.endswith('hourly.txt') and (yearmonth_str in tarinfo.name):
                        raw_weather_hourly.write(tar.extractfile(tarinfo).read())
                    elif tarinfo.name.endswith('daily.txt') and (yearmonth_str in tarinfo.name): 
                        # need this 2nd caveat above to handle ridiculous stuff like 200408.tar.gz containing 200512daily.txt for no reason
                        raw_weather_daily.write(tar.extractfile(tarinfo).read())
        else:
            if (self.debug==True):
                self.debug_outfile.write("extract: fpath is %s\n" % fpath)
            with zipfile.ZipFile(fpath, 'r') as zf:
                for name in zf.namelist():
                    if name.endswith('hourly.txt'):
                        raw_weather_hourly.write(zf.open(name).read())
                    elif name.endswith('daily.txt'):
                        raw_weather_daily.write(zf.open(name).read())
        return raw_weather_hourly, raw_weather_daily, file_type

    ########################################
    ########################################
    # Transformations of daily data e.g. '200704daily.txt' (from tarfile) or '201101daily.txt' (from zipfile)
    ########################################
    ########################################
    def _transform_daily(self, raw_weather, file_type,  weather_stations_list = None, banned_weather_stations_list=None, start_line=0, end_line=None):
        raw_weather.seek(0)
        # NOTE: not using UnicodeCSVReader because it.. (ironically) won't let us bail on a unicode error [mcc]
        #reader = UnicodeCSVReader(raw_weather)
        #header = reader.next() # skip header
        raw_header= raw_weather.readline() # skip header
        raw_header.strip()
        header = raw_header.split(',')
        header = [x.strip() for x in header]
        #print "header is ", header

        self.clean_observations_daily = StringIO()
        writer = UnicodeCSVWriter(self.clean_observations_daily)
        self.out_header = ["wban_code","date","temp_max","temp_min",
                           "temp_avg","departure_from_normal",
                           "dewpoint_avg", "wetbulb_avg","weather_types",
                           "snowice_depth", "snowice_waterequiv",
                           "snowfall","precip_total", "station_pressure",
                           "sealevel_pressure", 
                           "resultant_windspeed", "resultant_winddirection", "resultant_winddirection_cardinal",
                           "avg_windspeed",
                           "max5_windspeed", "max5_winddirection","max5_winddirection_cardinal",
                           "max2_windspeed", "max2_winddirection","max2_winddirection_cardinal"]
        writer.writerow(self.out_header)

        row_count = 0
        while True:
            try:
                #row = reader.next()
                # DIY csv parsing for QCLCD to avoid buffering issues in UnicodeCVSReader
                raw_row = raw_weather.readline()
                if (raw_row == ''):
                    break
                raw_row.strip()
                row = raw_row.split(',')
                
                self.current_row = row
                if (row_count % 100 == 0):
                    if (self.debug == True):
                        self.debug_outfile.write("\rdaily parsing: row_count=%06d" % row_count)
                        self.debug_outfile.flush()
             
                if (start_line > row_count):
                    row_count +=1
                    continue
                if ((end_line is not None) and (row_count > end_line)):
                    break
             
                row_count += 1
             
                if (len(row) == 0):
                    continue

                row_vals = getattr(self, '_parse_%s_row_daily' % file_type)(row, header, self.out_header)
             
                row_dict = dict(list(zip(self.out_header, row_vals)))
                if (weather_stations_list is not None):
                    # Only include observations from the specified list of wban_code values
                    if (row_dict['wban_code'] not in weather_stations_list):
                        continue
             
                writer.writerow(row_vals)
            except UnicodeDecodeError:
                if (self.debug == True):
                    self.debug_outfile.write("UnicodeDecodeError caught\n")
                    self.debug_outfile.write(str(row))
                    self.debug_outfile.write(str(row_count) + "\n" + str(list(zip(self.out_header,row))) + "\n")
                    self.debug_outfile.flush()
                # Skip this line, it has non-ASCII characters and probably shouldn't.
                # We may not get this error anymore (ironically) after rolling our own CSV parser
                #   as opposed to UnicodeCSVReeder
                pass
                
                continue
            except StopIteration:
                break

        self.debug_outfile.write('finished %s rows\n' % row_count)
        return self.clean_observations_daily


    def _parse_zipfile_row_daily(self, row, header, out_header):
        wban_code = row[header.index('WBAN')]
        date = row[header.index('YearMonthDay')] # e.g. 20140801
        temp_max = self.getTemp(row[header.index('Tmax')])
        temp_min = self.getTemp(row[header.index('Tmin')])
        temp_avg = self.getTemp(row[header.index('Tavg')])
        departure_from_normal = self.floatOrNA(row[header.index('Depart')])
        dewpoint_avg = self.floatOrNA(row[header.index('DewPoint')])
        wetbulb_avg = self.floatOrNA(row[header.index('WetBulb')])
        weather_types_list = self._parse_weather_types(row[header.index('CodeSum')])
        snowice_depth = self.getPrecip(row[header.index('Depth')])
        snowice_waterequiv = self.getPrecip(row[header.index('Water1')]) # predict 'heart-attack snow'!
        snowfall = self.getPrecip(row[header.index('SnowFall')])
        precip_total= self.getPrecip(row[header.index('PrecipTotal')])
        station_pressure=self.floatOrNA(row[header.index('StnPressure')])
        sealevel_pressure=self.floatOrNA(row[header.index('SeaLevel')])
        resultant_windspeed = self.floatOrNA(row[header.index('ResultSpeed')])
        resultant_winddirection, resultant_winddirection_cardinal=self.parse_wind(resultant_windspeed, row[header.index('ResultDir')])
        avg_windspeed=self.floatOrNA(row[header.index('AvgSpeed')])            
        max5_windspeed=self.floatOrNA(row[header.index('Max5Speed')])
        max5_winddirection, max5_winddirection_cardinal=self.parse_wind(max5_windspeed, row[header.index('Max5Dir')])
        max2_windspeed=self.floatOrNA(row[header.index('Max2Speed')])
        max2_winddirection, max2_winddirection_cardinal=self.parse_wind(max2_windspeed, row[header.index('Max2Dir')])

        vals = [wban_code,date,temp_max,temp_min,
                temp_avg,departure_from_normal,
                dewpoint_avg, wetbulb_avg,weather_types_list,
                snowice_depth, snowice_waterequiv,
                snowfall,precip_total, station_pressure,
                sealevel_pressure, 
                resultant_windspeed, resultant_winddirection, resultant_winddirection_cardinal,
                avg_windspeed,
                max5_windspeed, max5_winddirection,max5_winddirection_cardinal,
                max2_windspeed, max2_winddirection, max2_winddirection_cardinal]

        assert(len(out_header) == len(vals))
        
        return vals

    def _parse_tarfile_row_daily(self, row, header, out_header):
        wban_code = self.getWBAN(row[header.index('Wban Number')])
        date = row[header.index('YearMonthDay')] # e.g. 20140801
        temp_max = self.getTemp(row[header.index('Max Temp')])
        temp_min = self.getTemp(row[header.index('Min Temp')])
        temp_avg = self.getTemp(row[header.index('Avg Temp')])
        departure_from_normal = self.floatOrNA(row[header.index('Dep from Normal')])
        dewpoint_avg = self.floatOrNA(row[header.index('Avg Dew Pt')])
        wetbulb_avg = self.floatOrNA(row[header.index('Avg Wet Bulb')])
        weather_types_list = self._parse_weather_types(row[header.index('Significant Weather')])
        snowice_depth = self.getPrecip(row[header.index('Snow/Ice Depth')])
        snowice_waterequiv = self.getPrecip(row[header.index('Snow/Ice Water Equiv')]) # predict 'heart-attack snow'!
        snowfall = self.getPrecip(row[header.index('Precipitation Snowfall')])
        precip_total= self.getPrecip(row[header.index('Precipitation Water Equiv')])
        station_pressure=self.floatOrNA(row[header.index('Pressue Avg Station')]) # XXX Not me -- typo in header!
        sealevel_pressure=self.floatOrNA(row[header.index('Pressure Avg Sea Level')])
        resultant_windspeed = self.floatOrNA(row[header.index('Wind Speed')])
        resultant_winddirection, resultant_winddirection_cardinal=self.parse_wind(resultant_windspeed, row[header.index('Wind Direction')])
        avg_windspeed=self.floatOrNA(row[header.index('Wind Avg Speed')])            
        max5_windspeed=self.floatOrNA(row[header.index('Max 5 sec speed')])
        max5_winddirection, max5_winddirection_cardinal=self.parse_wind(max5_windspeed, row[header.index('Max 5 sec Dir')])
        max2_windspeed=self.floatOrNA(row[header.index('Max 2 min speed')])
        max2_winddirection, max2_winddirection_cardinal=self.parse_wind(max2_windspeed, row[header.index('Max 2 min Dir')])

        vals= [wban_code,date,temp_max,temp_min,
               temp_avg,departure_from_normal,
               dewpoint_avg, wetbulb_avg,weather_types_list,
               snowice_depth, snowice_waterequiv,
               snowfall,precip_total, station_pressure,
               sealevel_pressure, 
               resultant_windspeed, resultant_winddirection, resultant_winddirection_cardinal,
               avg_windspeed,
               max5_windspeed, max5_winddirection,max5_winddirection_cardinal,
               max2_windspeed, max2_winddirection, max2_winddirection_cardinal]

        assert(len(out_header) == len(vals))
        
        return vals


    ########################################
    ########################################
    # Transformations of hourly data e.g. 200704hourly.txt (from tarfile) or 201101hourly.txt (from zipfile)
    ########################################
    ########################################
    def _transform_hourly(self, raw_weather, file_type,  weather_stations_list = None,  banned_weather_stations_list=None, start_line=0, end_line=None):
        raw_weather.seek(0)
        # XXX mcc: should probably convert this to DIY CSV parsing a la _transform_daily()
        reader = UnicodeCSVReader(raw_weather)
        header = next(reader)
        # strip leading and trailing whitespace from header (e.g. from tarfiles)
        header = [x.strip() for x in header]

        self.clean_observations_hourly = StringIO()
        writer = UnicodeCSVWriter(self.clean_observations_hourly)
        self.out_header = ["wban_code","datetime","old_station_type","station_type", \
                           "sky_condition","sky_condition_top","visibility",\
                           "weather_types","drybulb_fahrenheit","wetbulb_fahrenheit",\
                           "dewpoint_fahrenheit","relative_humidity",\
                           "wind_speed","wind_direction","wind_direction_cardinal",\
                           "station_pressure","sealevel_pressure","report_type",\
                           "hourly_precip"]
        writer.writerow(self.out_header)

        row_count = 0
        while True:
            try:
                row = next(reader)
                if (row_count % 1000 == 0):
                    if (self.debug==True):
                        self.debug_outfile.write( "\rparsing: row_count=%06d" % row_count)
                        self.debug_outfile.flush()
             
                if (start_line > row_count):
                    row_count +=1
                    continue
                if ((end_line is not None) and (row_count > end_line)):
                    break
             
                row_count += 1
             
                if (len(row) == 0):
                    continue
             
                # this calls either self._parse_zipfile_row_hourly
                # or self._parse_tarfile_row_hourly
                row_vals = getattr(self, '_parse_%s_row_hourly' % file_type)(row, header, self.out_header)
                if (not row_vals):
                    continue
             
                row_dict = dict(list(zip(self.out_header, row_vals)))
                if (weather_stations_list is not None):
                    # Only include observations from the specified list of wban_code values
                    if (row_dict['wban_code'] not in weather_stations_list):
                        continue
                    
                if (banned_weather_stations_list is not None):
                    if (row_dict['wban_code'] in banned_weather_stations_list):
                        continue
                
                
             
                writer.writerow(row_vals)
            except FieldSizeLimitError:
                continue
            except StopIteration:
                break
        return self.clean_observations_hourly

    def _parse_zipfile_row_hourly(self, row, header, out_header):
        # There are two types of report types (column is called "RecordType" for some reason).
        # 1) AA - METAR (AVIATION ROUTINE WEATHER REPORT) - HOURLY
        # 2) SP - METAR SPECIAL REPORT
        # Special reports seem to occur at the same time (and have
        # largely the same content) as hourly reports, but under certain
        # adverse conditions (e.g. low visibility). 
        # As such, I believe it is sufficient to just use the 'AA' reports and keep
        # our composite primary key of (wban_code, datetime).
        report_type = row[header.index('RecordType')]

        wban_code = row[header.index('WBAN')]
        date = row[header.index('Date')] # e.g. 20140801
        time = row[header.index('Time')] # e.g. '601' 6:01am
        # pad this into a four digit number:
        time_str = None
        if (time):
            time_int =  self.integerOrNA(time)
            time_str = '%04d' % time_int
        
        weather_date = datetime.strptime('%s %s' % (date, time_str), '%Y%m%d %H%M')
        station_type = row[header.index('StationType')]
        old_station_type = None
        sky_condition = row[header.index('SkyCondition')]
        # Take the topmost atmospheric observation of clouds (e.g. in 'SCT013 BKN021 OVC029'
        # (scattered at 1300 feet, broken clouds at 2100 feet, overcast at 2900)
        # take OVC29 as the top layer.
        sky_condition_top = sky_condition.split(' ')[-1]
        visibility = self.floatOrNA(row[header.index('Visibility')])
        visibility_flag = row[header.index('VisibilityFlag')]
        # XX mcc consider handling visibility_flag =='s' for 'suspect'
        weather_types_list = self._parse_weather_types(row[header.index('WeatherType')])
        weather_types_flag = row[header.index('WeatherTypeFlag')]
        # XX mcc consider handling weather_type_flag =='s' for 'suspect'
        drybulb_F = self.floatOrNA(row[header.index('DryBulbFarenheit')])
        wetbulb_F = self.floatOrNA(row[header.index('WetBulbFarenheit')])
        dewpoint_F = self.floatOrNA(row[header.index('DewPointFarenheit')])
        rel_humidity = self.integerOrNA(row[header.index('RelativeHumidity')])
        wind_speed = self.integerOrNA(row[header.index('WindSpeed')])
        # XX mcc consider handling WindSpeedFlag == 's' for 'suspect'
        wind_direction, wind_cardinal = self.parse_wind(wind_speed, row[header.index('WindDirection')])
        station_pressure = self.floatOrNA(row[header.index('StationPressure')])
        sealevel_pressure = self.floatOrNA(row[header.index('SeaLevelPressure')])
        hourly_precip = self.getPrecip(row[header.index('HourlyPrecip')])
            
        vals= [wban_code,
               weather_date, 
               old_station_type,
               station_type,
               sky_condition, sky_condition_top,
               visibility, 
               weather_types_list,
               drybulb_F,
               wetbulb_F,
               dewpoint_F,
               rel_humidity,
               wind_speed, wind_direction, wind_cardinal,
               station_pressure, sealevel_pressure,
               report_type,
               hourly_precip]

        assert(len(out_header) == len(vals))

        # return hourly zipfile params
        return vals

    def _parse_tarfile_row_hourly(self, row, header, out_header):
        report_type = row[header.index('Record Type')]
        if (report_type == 'SP'):
            return None

        wban_code = row[header.index('Wban Number')]
        wban_code = wban_code.lstrip('0') # remove leading zeros from WBAN
        date = row[header.index('YearMonthDay')] # e.g. 20140801
        time = row[header.index('Time')] # e.g. '601' 6:01am
        # pad this into a four digit number:
        time_str = None
        if (time): 
            time_int = self.integerOrNA(time)
            if not time_int:
                time_str = None
                # XX: maybe just continue and bail if this doesn't work
                return None
            time_str = '%04d' % time_int
        try:
            weather_date = datetime.strptime('%s %s' % (date, time_str), '%Y%m%d %H%M')
        except ValueError:
            # This means the date / time can't be parsed and is probably not reliable.
            return None
        old_station_type = row[header.index('Station Type')].strip() # either AO1, AO2, or '-' (XX: why '-'??)
        station_type = None
        sky_condition = row[header.index('Sky Conditions')].strip()
        sky_condition_top = sky_condition.split(' ')[-1]
        
        visibility = self._parse_old_visibility(row[header.index('Visibility')])

        weather_types_list = self._parse_weather_types(row[header.index('Weather Type')])
        
        drybulb_F = self.floatOrNA(row[header.index('Dry Bulb Temp')])
        wetbulb_F = self.floatOrNA(row[header.index('Wet Bulb Temp')])
        dewpoint_F = self.floatOrNA(row[header.index('Dew Point Temp')])
        rel_humidity = self.integerOrNA(row[header.index('% Relative Humidity')])
        wind_speed = self.integerOrNA(row[header.index('Wind Speed (kt)')])
        wind_direction, wind_cardinal = self.parse_wind(wind_speed, row[header.index('Wind Direction')])
        station_pressure = self.floatOrNA(row[header.index('Station Pressure')])
        sealevel_pressure = self.floatOrNA(row[header.index('Sea Level Pressure')])
        hourly_precip = self.getPrecip(row[header.index('Precip. Total')])
        
        vals= [wban_code,
               weather_date, 
               old_station_type,station_type,
               sky_condition, sky_condition_top,
               visibility, 
               weather_types_list,
               drybulb_F,
               wetbulb_F,
               dewpoint_F,
               rel_humidity,
               wind_speed, wind_direction, wind_cardinal,
               station_pressure, sealevel_pressure,
               report_type,
               hourly_precip]

        assert(len(out_header) == len(vals))

        return vals

    # Help parse a 'present weather' string like 'FZFG' (freezing fog) or 'BLSN' (blowing snow) or '-RA' (light rain)
    # When we are doing precip slurp as many as possible
    def _do_weather_parse(self, pw, mapping, multiple=False, local_debug=False):

        # Grab as many of the keys as possible consecutively in the string
        retvals = []
        while (multiple == True):
            (pw, key) = self._do_weather_parse(pw, mapping, multiple=False, local_debug=True)
            #print "got pw, key=", pw,key
            retvals.append(key)
            if ((pw == '') or (key == 'NULL')):
                return pw, retvals
                break
            else:
                continue

        if (len(pw) == 0): 
            return ('', 'NULL')

        # 2nd parse for descriptors
        for (key, val) in mapping:
            #print "key is '%s'" % key
            q = pw[0:len(key)]
            if (q == key):
                #print "key found: ", q
                pw2=pw[len(key):]
                #print "returning", l2
                #return (l2, val)
                return (pw2, key)
        return (pw, 'NULL')

    # Parse a 'present weather' string like 'FZFG' (freezing fog) or 'BLSN' (blowing snow) or '-RA' (light rain)
    def _parse_present_weather(self, pw):
        orig_pw = pw
        l = pw

        intensities =  [('-','Light'),
                        ('+','Heavy')]

        (l, intensity) = self._do_weather_parse(l, intensities)

        vicinities = [('VC','Vicinity')]
        (l, vicinity) = self._do_weather_parse(l, vicinities)
        
        descriptors = [('MI','Shallow'),
                       ('PR','Partial'),
                       ('BC','Patches'),
                       ('DR','Low Drifting'),
                       ('BL','Blowing'),
                       ('SH','Shower(s)'),
                       ('TS','Thunderstorm'),
                       ('FZ','Freezing')]
            
        (l, desc)= self._do_weather_parse(l, descriptors)
        
        # 3rd parse for phenomena
        precip_phenoms= [('DZ','Drizzle'),
                         ('RA','Rain'),
                         ('SN','Snow'),
                         ('SG','Snow Grains'),
                         ('IC','Ice Crystals'),
                         ('PE','Ice Pellets'),
                         ('PL','Ice Pellets'),
                         ('GR','Hail'),
                         ('GS','Small Hail'),
                         ('UP','Unknown Precipitation')]
        # We use arrays instead of hashmaps because we want to look for FG+ before FG (sigh)
        obscuration_phenoms  = [('BR','Mist'),
                                ('FG+','Heavy Fog'),
                                ('FG','Fog'),
                                ('FU','Smoke'),
                                ('VA','Volcanic Ash'),
                                ('DU','Widespread Dust'),
                                ('SA','Sand'),
                                ('HZ','Haze'),
                                ('PY','Spray')]
        other_phenoms = [('PO','Dust Devils'),
                         ('SQ','Squalls'),
                         ('FC','Funnel Cloud'),
                         ('+FC','Tornado Waterspout'),
                         ('SS','Sandstorm'),
                         ('DS','Duststorm'),
                         ('GL','Glaze')]
                
        (l, precips) = self._do_weather_parse(l, precip_phenoms, multiple =True)
        (l, obscuration) = self._do_weather_parse(l, obscuration_phenoms)
        (l, other) = self._do_weather_parse(l, other_phenoms)

        # if l still has a length let's print it out and see what went wrong
        if (self.debug==True):
            if (len(l) > 0):
                self.debug_outfile.write("\n")
                self.debug_outfile.write(str(self.current_row))
                self.debug_outfile.write("\ncould not fully parse present weather : '%s' '%s'\n\n" % ( orig_pw, l))
        wt_list = [intensity, vicinity, desc, precips[0], obscuration, other]
    
        ret_wt_lists = []
        ret_wt_lists.append(wt_list)
        
        #if (len(precips) > 1):
        #    print "first precip: ", wt_list
        for p in precips[1:]:
            if p != 'NULL':
                #print "extra precip!", p, orig_pw
                wt_list = ['NULL', 'NULL', 'NULL', p, 'NULL', 'NULL']
                #print "extra precip (precip):", wt_list
                ret_wt_lists.append(wt_list)
        
        return ret_wt_lists
        


    # Parse a list of 'present weather' strings and convert to multidimensional postgres array.
    def _parse_weather_types(self, wt_str):
        wt_str = wt_str.strip()
        if ((wt_str == '') or (wt_str == '-')):
            return None
        if (not wt_str):
            return None
        else:
            wt_list = wt_str.split(' ')
            wt_list = [wt.strip() for wt in wt_list]
            pw_lists = []

            for wt in wt_list:
                wts = self._parse_present_weather(wt)
                # make all weather reports have the same length..
                for obsv in wts:
                    wt_list3 = self.list_to_postgres_array(obsv)
                    pw_lists.append(wt_list3)
            list_of_lists = "{" +  ', '.join(pw_lists) + "}"
            #print "list_of_lists: "  , list_of_lists
            return list_of_lists

    def _parse_old_visibility(self, visibility_str):
        visibility_str = visibility_str.strip()
        
        visibility_str = re.sub('SM', '', visibility_str)
        # has_slash = re.match('\/'), visibility_str)
        # XX This is not worth it, too many weird, undocumented special cases on this particular column
        return None


    # list_to_postgres_array(list_string): convert to {blah, blah2, blah3} format for postgres.
    def list_to_postgres_array(self, l):
        return "{" +  ', '.join(l) + "}"

    def getWBAN(self,wban):
        return wban
    
    def getTemp(self, temp):
        if temp[-1] == '*':
            temp = temp[:-1]
        return self.floatOrNA(temp)
        
    def getWind(self, wind_speed, wind_direction):
        wind_cardinal = None
        wind_direction = wind_direction.strip()
        if (wind_direction == 'VR' or wind_direction =='M' or wind_direction=='VRB'):
            wind_direction='VRB'
            wind_cardinal = 'VRB'
        elif (wind_direction == '' or wind_direction == '-'):
            wind_direction =None
            wind_cardinal = None
        else:
            wind_direction_int = None
            try:
                # XXX: NOTE: rounding wind_direction to integer. Sorry.
                # XXX: Examine this field more carefully to determine what its range is.
                wind_direction_int = int(round(float(wind_direction)))
                wind_direction = wind_direction_int
            except ValueError as e:
                if (self.debug==True):
                    if (self.current_row): 
                        self.debug_outfile.write("\n")                        
                        zipped_row = list(zip(self.out_header,self.current_row))
                        for column in zipped_row:
                            self.debug_outfile.write(str(column) + "\n")
                    self.debug_outfile.write("\nValueError: [%s], could not convert wind_direction '%s' to int\n" % (e, wind_direction))
                    self.debug_outfile.flush()
                return None, None

            wind_cardinal = get_cardinal_direction(wind_direction_int)
        if (wind_speed == 0):
            wind_direction = None
            wind_cardinal = None
        return wind_direction, wind_cardinal

        
    def getPrecip(self, precip_str):
        precip_total = None
        precip_total = precip_str.strip()
        if (precip_total == 'T'):
            precip_total = .005 # 'Trace' precipitation = .005 inch or less
        precip_total = self.floatOrNA(precip_total)
        return precip_total
                        
    def floatOrNA(self, val):
        val_str = str(val).strip()
        if (val_str == 'M'):
            return None
        if (val_str == '-'):
            return None
        if (val_str == 'err'):
            return None
        if (val_str == 'null'):
            return None
        if (val_str == ''):  # WindSpeed line
            return None
        else:
            try:
                fval = float(val_str)
            except ValueError as e:
                if (self.debug==True):
                    if (self.current_row): 
                        self.debug_outfile.write("\n")
                        zipped_row = list(zip(self.out_header,self.current_row))
                        for column in zipped_row:
                            self.debug_outfile.write(str(column)+ "\n")
                    self.debug_outfile.write("\nValueError: [%s], could not convert '%s' to float\n" % (e, val_str))
                    self.debug_outfile.flush()
                return None
            return fval

    def integerOrNA(self, val):
        val_str = str(val).strip()
        if (val_str == 'M'):
            return None
        if (val_str == '-'):
            return None
        if (val_str == 'VRB'):
            return None
        if (val_str == 'err'):
            return None
        if (val_str == 'null'):
            return None
        if (val_str.strip() == ''):  # WindSpeed line
            return None
        else:
            try: 
                ival = int(val)
            except ValueError as e:
                if (self.debug==True):
                    if (self.current_row): 
                        self.debug_outfile.write("\n")
                        zipped_row = list(zip(self.out_header,self.current_row))
                        for column in zipped_row:
                            self.debug_outfile.write(str(column) + "\n")
                    self.debug_outfile.write("\nValueError [%s] could not convert '%s' to int\n" % (e, val))
                    self.debug_outfile.flush()
                return None
            return ival

    def _make_daily_table(self):
        self.daily_table = self._get_daily_table()
        self.daily_table.append_column(Column('id', BigInteger, primary_key=True))
        self.daily_table.create(engine, checkfirst=True)

    def _make_hourly_table(self):
        self.hourly_table = self._get_hourly_table()
        self.hourly_table.append_column(Column('id', BigInteger, primary_key=True))
        self.hourly_table.create(engine, checkfirst=True)

    def _make_metar_table(self):
        self.metar_table = self._get_metar_table()
        self.metar_table.append_column(Column('id', BigInteger, primary_key=True))
        self.metar_table.create(engine, checkfirst=True)
        
    def _get_daily_table(self, name='dat'):
        return Table('%s_weather_observations_daily' % name, Base.metadata,
                            Column('wban_code', String(5), nullable=False),
                            Column('date', Date, nullable=False),
                            Column('temp_max', Float, index=True),
                            Column('temp_min', Float, index=True),
                            Column('temp_avg', Float, index=True),
                            Column('departure_from_normal', Float),
                            Column('dewpoint_avg', Float),
                            Column('wetbulb_avg', Float),
                            #Column('weather_types', ARRAY(String(16))), # column 'CodeSum',
                            Column('weather_types', ARRAY(String)), # column 'CodeSum',
                            Column("snowice_depth", Float),
                            Column("snowice_waterequiv", Float),
                            # XX: Not sure about meaning of 'Cool' and 'Heat' columns in daily table,
                            #     based on documentation.
                            Column('snowfall', Float),
                            Column('precip_total', Float, index=True),
                            Column('station_pressure', Float),
                            Column('sealevel_pressure', Float),
                            Column('resultant_windspeed', Float),
                            Column('resultant_winddirection', String(3)), # appears to be 00 (000) to 36 (360)
                            Column('resultant_winddirection_cardinal', String(3)), # e.g. NNE, NNW
                            Column('avg_windspeed', Float),
                            Column('max5_windspeed', Float),
                            Column('max5_winddirection', String(3)), # 000 through 360, M for missing
                            Column('max5_direction_cardinal', String(3)), # e.g. NNE, NNW
                            Column('max2_windspeed', Float), 
                            Column('max2_winddirection', String(3)), # 000 through 360, M for missing
                            Column('max2_direction_cardinal', String(3)), # e.g. NNE, NNW
                            Column('longitude', Float),
                            Column('latitude', Float),
                            keep_existing=True)

    # todo: covered by odo.resource("dat_weather_observations_hourly")
    # def _get_hourly_table(self, name='dat'):
    #     return Table('%s_weather_observations_hourly' % name, Base.metadata,
    #             Column('wban_code', String(5), nullable=False),
    #             Column('datetime', DateTime, nullable=False),
    #             # AO1: without precipitation discriminator, AO2: with precipitation discriminator
    #             Column('old_station_type', String(5)),
    #             Column('station_type', Integer),
    #             Column('sky_condition', String),
    #             Column('sky_condition_top', String), # top-level sky condition, e.g.
    #                                                     # if 'FEW018 BKN029 OVC100'
    #                                                     # we have overcast at 10,000 feet (100 * 100).
    #                                                     # BKN017TCU means broken clouds at 1700 feet w/ towering cumulonimbus
    #                                                     # BKN017CB means broken clouds at 1700 feet w/ cumulonimbus
    #             Column('visibility', Float), #  in Statute Miles
    #             # XX in R: unique(unlist(strsplit(unlist(as.character(unique(x$WeatherType))), ' ')))
    #             #Column('weather_types', ARRAY(String(16))),
    #             Column('weather_types', ARRAY(String)),
    #             Column('drybulb_fahrenheit', Float, index=True), # These can be NULL bc of missing data
    #             Column('wetbulb_fahrenheit', Float), # These can be NULL bc of missing data
    #             Column('dewpoint_fahrenheit', Float),# These can be NULL bc of missing data
    #             Column('relative_humidity', Integer),
    #             Column('wind_speed', Integer),
    #             Column('wind_direction', String(3)), # 000 to 360
    #             Column('wind_direction_cardinal', String(3)), # e.g. NNE, NNW
    #             Column('station_pressure', Float),
    #             Column('sealevel_pressure', Float),
    #             Column('report_type', String), # Either 'AA' or 'SP'
    #             Column('hourly_precip', Float, index=True),
    #             Column('longitude', Float),
    #             Column('latitude', Float),
    #             keep_existing=True)

    # todo: covered by odo.resource("dat_weather_observations_metar")
    # def _get_metar_table(self, name='dat'):
    #     return Table('%s_weather_observations_metar' % name, Base.metadata,
    #             Column('wban_code', String(5), nullable=False),
    #             Column('call_sign', String(5), nullable=False),
    #             Column('datetime', DateTime, nullable=False),
    #             Column('sky_condition', String),
    #             Column('sky_condition_top', String), # top-level sky condition, e.g.
    #                                                     # if 'FEW018 BKN029 OVC100'
    #                                                     # we have overcast at 10,000 feet (100 * 100).
    #                                                     # BKN017TCU means broken clouds at 1700 feet w/ towering cumulonimbus
    #                                                     # BKN017CB means broken clouds at 1700 feet w/ cumulonimbus
    #             Column('visibility', Float), #  in Statute Miles
    #             Column('weather_types', ARRAY(String)),
    #             Column('temp_fahrenheit', Float, index=True), # These can be NULL bc of missing data
    #             Column('dewpoint_fahrenheit', Float),# These can be NULL bc of missing data
    #             Column('wind_speed', Integer),
    #             Column('wind_direction', String(3)), # 000 to 360
    #             Column('wind_direction_cardinal', String(3)), # e.g. NNE, NNW
    #             Column('wind_gust', Integer),
    #             Column('station_pressure', Float),
    #             Column('sealevel_pressure', Float),
    #             Column('precip_1hr', Float, index=True),
    #             Column('precip_3hr', Float, index=True),
    #             Column('precip_6hr', Float, index=True),
    #             Column('precip_24hr', Float, index=True),
    #             Column('longitude', Float),
    #             Column('latitude', Float),
    #             keep_existing=True)

    def _extract_fname(self, year_num, month_num):
        self.current_year = year_num
        self.current_month = month_num
        curr_dt = datetime(year_num, month_num, 1, 0, 0)
        if ((year_num < 2007) or (year_num == 2007 and month_num < 5)):
            tar_filename =  '%s.tar.gz' % (curr_dt.strftime('%Y%m'))
            return tar_filename
        else:
            zip_filename = 'QCLCD%s.zip' % curr_dt.strftime('%Y%m')
            return zip_filename

    def _extract_fnames(self):
        tar_start = datetime(1996, 7, 1, 0, 0)
        tar_end = datetime(2007, 5, 1, 0, 0)
        zip_start = datetime(2007, 5, 1, 0, 0)
        zip_end = datetime.now() + timedelta(days=30)
        tar_filenames = ['%s.tar.gz' % d.strftime('%Y%m') for d in \
            self._date_span(tar_start, tar_end)]
        zip_filenames = ['QCLCD%s.zip' % d.strftime('%Y%m') for d in \
            self._date_span(zip_start, zip_end)]
        return tar_filenames + zip_filenames

    def _load_hourly(self, transformed_input):
        if (self.debug==True):
            transformed_input.seek(0) 
            f = open(os.path.join(self.data_dir, 'weather_etl_dump_hourly.txt'), 'w')
            f.write(transformed_input.getvalue())
            f.close()
        transformed_input.seek(0)
        self.src_hourly_table = self._get_hourly_table(name='src')
        self.src_hourly_table.drop(engine, checkfirst=True)
        self.src_hourly_table.create(engine, checkfirst=True)

        skip_cols = ['id', 'latitude', 'longitude']
        names = [c.name for c in self.hourly_table.columns if c.name not in skip_cols]
        ins_st = "COPY src_weather_observations_hourly ("
        for idx, name in enumerate(names):
            if idx < len(names) - 1:
                ins_st += '%s, ' % name
            else:
                ins_st += '%s)' % name
        else:
            ins_st += "FROM STDIN WITH (FORMAT CSV, HEADER TRUE, DELIMITER ',')"
        conn = engine.raw_connection()
        cursor = conn.cursor()
        if (self.debug==True): 
            self.debug_outfile.write("\nCalling: '%s'\n" % ins_st)
            self.debug_outfile.flush()
        cursor.copy_expert(ins_st, transformed_input)

        conn.commit()
        if (self.debug==True):
            self.debug_outfile.write("Committed: '%s'" % ins_st)
            self.debug_outfile.flush()


    def _load_daily(self, transformed_input): 
        if (self.debug==True):
            transformed_input.seek(0) 
            f = open(os.path.join(self.data_dir, 'weather_etl_dump_daily.txt'), 'w')
            f.write(transformed_input.getvalue())
            f.close()
        transformed_input.seek(0)

        skip_cols = ['id', 'latitude', 'longitude']
        names = [c.name for c in self.daily_table.columns if c.name not in skip_cols]
        self.src_daily_table = self._get_daily_table(name='src')
        self.src_daily_table.drop(engine, checkfirst=True)
        self.src_daily_table.create(engine, checkfirst=True)
        ins_st = "COPY src_weather_observations_daily ("
        for idx, name in enumerate(names):
            if idx < len(names) - 1:
                ins_st += '%s, ' % name
            else:
                ins_st += '%s)' % name
        else:
            ins_st += "FROM STDIN WITH (FORMAT CSV, HEADER TRUE, DELIMITER ',')"
        conn = engine.raw_connection()
        cursor = conn.cursor()
        if (self.debug==True): 
            self.debug_outfile.write("\nCalling: '%s'\n" % ins_st)
            self.debug_outfile.flush()
        cursor.copy_expert(ins_st, transformed_input)

        conn.commit()
        if (self.debug == True):
            self.debug_outfile.write("committed: '%s'" % ins_st)
            self.debug_outfile.flush()

    def _date_span(self, start, end):
        delta = timedelta(days=30)
        while (start.year, start.month) != (end.year, end.month):
            yield start
            start = self._add_month(start)
    
    def _add_month(self, sourcedate):
        month = sourcedate.month
        year = sourcedate.year + month / 12
        month = month %12 + 1
        day = min(sourcedate.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def _get_distinct_weather_stations_by_month(self,year, month, daily_or_hourly='daily'):
        table = Table('dat_weather_observations_%s' % daily_or_hourly, Base.metadata, autoload=True, autoload_with=engine)
        column = None
        if (daily_or_hourly == 'daily'):
            column = table.c.date
        elif (daily_or_hourly == 'hourly'):
            column = table.c.datetime
        
        dt = datetime(year, month,0o1)
        dt_nextmonth = dt + relativedelta.relativedelta(months=1)
        
        q = session.query(distinct(table.c.wban_code)).filter(and_(column >= dt,
                                                                   column < dt_nextmonth))
        
        station_list = list(map(operator.itemgetter(0), q.all()))
        return station_list

    # Given that this was the most recent month, year, call this function,
    # which will figure out the most recent hourly weather observation and
    # delete all metars before that datetime.


# todo: refactor
def clear_metars() -> None:
    """Remove all rows in the metar table older than the latest row in the
    hourly table."""

    # build a datetime and then remove all metars after the max datetime
    hourlies = reflect("dat_weather_observations_hourly", Base.meta, engine)
    metars = reflect("dat_weather_observations_metar", Base.meta, engine)
    # sql = "SELECT max (datetime) from dat_weather_observations_hourly;"
    # given this time, delete all from dat_weather_observations_metar
    #
    # print(("executing: ", sql))
    # conn = engine.contextual_connect()
    # results = conn.execute(sql)
    selection = select([func.max(hourlies.datetime)])
    result = engine.execute(selection).scalar()
    max_datetime = datetime.strftime(result, "%Y-%m-%d %H:%M:%S")
    # if not res:
    #     return
    # res_dt = res[0]
    # given this most recent time, delete any metars from before that time
    deletion = delete()
    sql2 = "DELETE FROM dat_weather_observations_metar WHERE datetime < '%s'" % (res_dt_str)
    print(("executing: " , sql2))
    results = conn.execute(sql2)
