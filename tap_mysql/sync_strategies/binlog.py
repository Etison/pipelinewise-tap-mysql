#!/usr/bin/env python3
# pylint: disable=missing-function-docstring,too-many-arguments,too-many-branches
import codecs
import copy
import datetime
import json
import re
import random

import pymysql.connections
import pymysql.err
import pytz
import singer
import tzlocal

from typing import Dict
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.constants import FIELD_TYPE
from pymysqlreplication.event import RotateEvent
from pymysqlreplication.row_event import (
    DeleteRowsEvent,
    UpdateRowsEvent,
    WriteRowsEvent,
)
from singer import utils, Schema

from hashlib import sha1

import tap_mysql.sync_strategies.common as common
from tap_mysql.stream_utils import write_schema_message, get_key_properties
from tap_mysql.discover_utils import discover_catalog, desired_columns
from tap_mysql.connection import connect_with_backoff, make_connection_wrapper

LOGGER = singer.get_logger('tap_mysql')


SDC_DELETED_AT = "_sdc_deleted_at"
SYS_UPDATED_AT = "_sys_updated_at"
SYS_EVENT_TYPE = "_sys_event_type"
SYS_HASHDIFF   = "_sys_diffkey"
SYS_HASHKEY    = "_sys_hashkey"
SYS_LOG_FILE   = "_sys_log_file"
SYS_LOG_POS    = "_sys_log_position"
SYS_LINENO     = "_sys_transaction_lineno"

INSERT_EVENT = 1
UPDATE_EVENT = 2
DELETE_EVENT = 3

UPDATE_BOOKMARK_PERIOD = 1000

BOOKMARK_KEYS = {'log_file', 'log_pos', 'version', 'timestamp'}

MYSQL_TIMESTAMP_TYPES = {
    FIELD_TYPE.TIMESTAMP,
    FIELD_TYPE.TIMESTAMP2
}


def add_automatic_properties(catalog_entry, columns):
    catalog_entry.schema.properties[SDC_DELETED_AT] = Schema(
        type=["null", "string"],
        format="date-time"
    )

    catalog_entry.schema.properties[SYS_UPDATED_AT] = Schema(
        type=["null", "string"],
        format="date-time"
    )

    catalog_entry.schema.properties[SYS_EVENT_TYPE] = Schema(
        type="integer"
    )

    catalog_entry.schema.properties[SYS_HASHDIFF] = Schema(
            type=["null", "string"],
            format="date-time"
    )

    catalog_entry.schema.properties[SYS_HASHKEY] = Schema(
            type=["null", "string"],
            format="date-time"
    )

    catalog_entry.schema.properties[SYS_LOG_POS] = Schema(
        type='integer'
    )

    catalog_entry.schema.properties[SYS_LOG_FILE] = Schema(
        type='integer'
    )


    catalog_entry.schema.properties[SYS_LINENO] = Schema(
        type='integer'
    )



    columns.append(SDC_DELETED_AT)
    columns.append(SYS_UPDATED_AT)
    columns.append(SYS_EVENT_TYPE)
    columns.append(SYS_HASHKEY)
    columns.append(SYS_HASHDIFF)
    columns.append(SYS_LINENO)
    columns.append(SYS_LOG_POS)
    columns.append(SYS_LOG_FILE)

    return columns


def verify_binlog_config(mysql_conn):
    with connect_with_backoff(mysql_conn) as open_conn:
        with open_conn.cursor() as cur:
            cur.execute("SELECT  @@binlog_format")
            binlog_format = cur.fetchone()[0]

            if binlog_format != 'ROW':
                raise Exception(f"Unable to replicate binlog stream because binlog_format is "
                                f"not set to 'ROW': {binlog_format}.")

            try:
                cur.execute("SELECT  @@binlog_row_image")
                binlog_row_image = cur.fetchone()[0]
            except pymysql.err.InternalError as ex:
                if ex.args[0] == 1193:
                    raise Exception("Unable to replicate binlog stream because binlog_row_image "
                                    "system variable does not exist. MySQL version must be at "
                                    "least 5.6.2 to use binlog replication.")
                raise ex

            if binlog_row_image != 'FULL':
                raise Exception(f"Unable to replicate binlog stream because binlog_row_image is "
                                f"not set to 'FULL': {binlog_row_image}.")


def verify_log_file_exists(mysql_conn, log_file, log_pos):
    with connect_with_backoff(mysql_conn) as open_conn:
        with open_conn.cursor() as cur:
            cur.execute("SHOW BINARY LOGS")
            result = cur.fetchall()

            existing_log_file = list(filter(lambda log: log[0] == log_file, result))

            if not existing_log_file:
                raise Exception(f"Unable to replicate binlog stream because log file {log_file} does not exist.")

            current_log_pos = existing_log_file[0][1]

            if log_pos > current_log_pos:
                raise Exception(f"Unable to replicate binlog stream because requested position ({log_pos}) "
                                f"for log file {log_file} is greater than current position ({current_log_pos}). ")


def fetch_current_log_file_and_pos(mysql_conn):
    with connect_with_backoff(mysql_conn) as open_conn:
        with open_conn.cursor() as cur:
            cur.execute("SHOW MASTER STATUS")

            result = cur.fetchone()

            if result is None:
                raise Exception("MySQL binary logging is not enabled.")

            current_log_file, current_log_pos = result[0:2]

            return current_log_file, current_log_pos


def fetch_server_id(mysql_conn):
    with connect_with_backoff(mysql_conn) as open_conn:
        with open_conn.cursor() as cur:
            cur.execute("SELECT @@server_id")
            server_id = cur.fetchone()[0]

            return server_id


def json_bytes_to_string(data):
    if isinstance(data, bytes):  return data.decode()
    if isinstance(data, dict):   return dict(map(json_bytes_to_string, data.items()))
    if isinstance(data, tuple):  return tuple(map(json_bytes_to_string, data))
    if isinstance(data, list):   return list(map(json_bytes_to_string, data))
    return data


def row_to_singer_record(catalog_entry, version, db_column_map, row, time_extracted):
    row_to_persist = {}

    LOGGER.debug('Schema properties: %s',catalog_entry.schema.properties)
    LOGGER.debug('Event columns: %s', db_column_map)

    key_properties = get_key_properties(catalog_entry)

    for column_name, val in row.items():
        property_type = catalog_entry.schema.properties[column_name].type
        property_format = catalog_entry.schema.properties[column_name].format
        db_column_type = db_column_map.get(column_name)

        if isinstance(val, datetime.datetime):
            if db_column_type in MYSQL_TIMESTAMP_TYPES:
                # The mysql-replication library creates datetimes from TIMESTAMP columns using fromtimestamp which
                # will use the local timezone thus we must set tzinfo accordingly See:
                # https://github.com/noplay/python-mysql-replication/blob/master/pymysqlreplication/row_event.py#L143
                # -L145
                timezone = tzlocal.get_localzone()
                local_datetime = timezone.localize(val)
                utc_datetime = local_datetime.astimezone(pytz.UTC)
                row_to_persist[column_name] = utc_datetime.isoformat()
            else:
                row_to_persist[column_name] = val.isoformat()

        elif isinstance(val, datetime.date):
            row_to_persist[column_name] = val.isoformat() + 'T00:00:00+00:00'

        elif isinstance(val, datetime.timedelta):
            if property_format == 'time':
                # this should convert time column into 'HH:MM:SS' formatted string
                row_to_persist[column_name] = str(val)
            else:
                timedelta_from_epoch = datetime.datetime.utcfromtimestamp(0) + val
                row_to_persist[column_name] = timedelta_from_epoch.isoformat() + '+00:00'

        elif db_column_type == FIELD_TYPE.JSON:
            row_to_persist[column_name] = json.dumps(json_bytes_to_string(val))

        elif isinstance(val, bytes):
            if column_name == 'additional_info':
                # Additional_info has a bad header in it
                row_to_persist[column_name] = codecs.encode(val, 'hex').decode('utf-8')[5:2**16-1]
            else:
                row_to_persist[column_name] = codecs.encode(val, 'hex').decode('utf-8')[:2**16-1]

        elif 'boolean' in property_type or property_type == 'boolean':
            if val is None:
                boolean_representation = None
            elif val == 0:
                boolean_representation = False
            elif db_column_type == FIELD_TYPE.BIT:
                boolean_representation = int(val) != 0
            else:
                boolean_representation = True
            row_to_persist[column_name] = boolean_representation
        elif val is not None and (column_name.startswith('html') or db_column_type == 'longtext'):
            row_to_persist[column_name] = val[:2**16-1]
        else:
            row_to_persist[column_name] = val

    row_to_persist[SYS_HASHKEY] = calculate_hashkey(row_to_persist, key_properties)
    row_to_persist[SYS_HASHDIFF] = calculate_hashdiff(row_to_persist, key_properties)

    return singer.RecordMessage(
        stream=catalog_entry.stream,
        record=row_to_persist,
        version=version,
        time_extracted=time_extracted)


def get_min_log_pos_per_log_file(binlog_streams_map, state):
    min_log_pos_per_file = {}

    for tap_stream_id, bookmark in state.get('bookmarks', {}).items():
        stream = binlog_streams_map.get(tap_stream_id)

        if not stream:
            continue

        log_file = bookmark.get('log_file')
        log_pos = bookmark.get('log_pos')

        if not min_log_pos_per_file.get(log_file):
            min_log_pos_per_file[log_file] = {
                'log_pos': log_pos,
                'streams': [tap_stream_id]
            }

        elif min_log_pos_per_file[log_file]['log_pos'] > log_pos:
            min_log_pos_per_file[log_file]['log_pos'] = log_pos
            min_log_pos_per_file[log_file]['streams'].append(tap_stream_id)

        else:
            min_log_pos_per_file[log_file]['streams'].append(tap_stream_id)

    return min_log_pos_per_file


def calculate_bookmark(mysql_conn, binlog_streams_map, state):
    min_log_pos_per_file = get_min_log_pos_per_log_file(binlog_streams_map, state)

    with connect_with_backoff(mysql_conn) as open_conn:
        with open_conn.cursor() as cur:
            cur.execute("SHOW BINARY LOGS")

            binary_logs = cur.fetchall()

            if binary_logs:
                server_logs_set = {log[0] for log in binary_logs}
                state_logs_set = set(min_log_pos_per_file.keys())
                expired_logs = state_logs_set.difference(server_logs_set)

                if expired_logs:
                    raise Exception('Unable to replicate binlog stream because the following binary log(s) no longer '
                                    f'exist: {", ".join(expired_logs)}')

                for log_file in sorted(server_logs_set):
                    if min_log_pos_per_file.get(log_file):
                        return log_file, min_log_pos_per_file[log_file]['log_pos']

            raise Exception("Unable to replicate binlog stream because no binary logs exist on the server.")


def update_bookmarks(state, binlog_streams_map, new_state):
    for tap_stream_id in binlog_streams_map.keys():
        for k,v in new_state.items():
            state = singer.write_bookmark(state,
                                      tap_stream_id,
                                      k, v)

    return state


def get_db_column_types(event):
    return {c.name: c.type for c in event.columns}


def _join_hashes(values):
    '''
    _join_hashes will take an input value stream and sha1 them together

    We will ignore blank strings and None mainly because if there's an added column in the future this will change

    '''

    def encode(x):
        s = str(x).encode('utf-8').strip()

        if x is None or s == b'':
            return ''
        else:
            return sha1(s).hexdigest()

    return sha1(''.join([encode(v) for v in values]).encode('utf-8')).hexdigest()

def calculate_hashdiff(record, key_properties):
    '''
    Hash diff:
        Every column minus the id column and metadata columns (everything with an underscore) + _sys_deleted_at
    '''

    keys = list(
        filter(
            lambda x: x[0:4] not in ('_sys', '_sdc') and x[0:3] != ('_is'),
            set(sorted(record.keys())) - set(key_properties)
        )
    )

    keys = sorted(keys)

    return _join_hashes([record[k] for k in keys])

def calculate_hashkey(record, key_properties):
    '''
    Hash Key
    Hash key = id
        Unique constraint: id + _sys_updated_at
    '''

    keys = set(key_properties) | set([SYS_UPDATED_AT])

    return _join_hashes([record[k] for k in sorted(keys)])

def _join_hashes_sql(properties):
    '''
    Datetimes
    Ints
    ??
    '''
    
    def encode(column, _type):
        _format = _type.get('format', False)
        _type = set(_type['type']) - set(['null'])
        
        assert len(_type) == 1
        
        _type = list(_type)[0]
        
        encode_stmt = ''
        if _type == 'boolean':
            true = sha1('True'.encode('utf-8')).hexdigest()
            false = sha1('False'.encode('utf-8')).hexdigest()
            encode_stmt = f"(CASE WHEN {column} AND {column} IS NOT NULL THEN '{true}' ELSE '{false}' END)"
        elif _format == 'date-time' and _type == 'string' and column == '_sys_updated_at':
            encode_stmt = f"(CASE WHEN {column} IS NOT NULL THEN SHA1(to_char({column}, 'YYYY-MM-DD\"T\"HH24:MI:SS+00:00')) ELSE '' END)"
        elif _format == 'date-time' and _type == 'string' and column != '_sys_updated_at':
            encode_stmt = f"(CASE WHEN {column} IS NOT NULL THEN SHA1(to_char({column}, 'YYYY-MM-DD\"T\"HH24:MI:SS')) ELSE '' END)"
        elif _type == 'string':
            encode_stmt = f"(CASE WHEN TRIM(COALESCE({column}, '')) <> '' THEN SHA1({column}) ELSE '' END)"
        elif _type == 'integer':
            encode_stmt = f"(CASE WHEN {column} IS NOT NULL THEN SHA1({column}::text) ELSE '' END)"
        else:
            raise Exception("Unknown Type {}".format(_type))
        return encode_stmt
    
    sql = " || ".join([
        encode(k, properties[k])
        for k in sorted(properties.keys())
    ])
    
    return "SHA1({})".format(sql)

def calculate_hashkey_sql(catalog_entry):
    key_properties = get_key_properties(catalog_entry)
    
    keys = set(key_properties) | set([SYS_UPDATED_AT])
    
    schema = catalog_entry.schema.to_dict()['properties']
    
    properties = {
        k: schema[k]
        for k in key_properties
    }
    
    properties['_sys_updated_at'] = {'type': ['string'], 'format': 'date-time'}
    
    return _join_hashes_sql(properties)

def calculate_hashdiff_sql(catalog_entry):
    key_properties = get_key_properties(catalog_entry)
    schema = catalog_entry.schema.to_dict()['properties']
    
    properties = catalog_entry.schema.to_dict()['properties'].keys()
    
    keys = list(
        filter(
            lambda x: x[0:4] not in ('_sys', '_sdc') and x[0:3] != ('_is'),
            set(sorted(properties)) - set(key_properties)
        )
    )
    
    properties = {
        k: schema[k]
        for k in keys
    }
    
    return _join_hashes_sql(properties)



def handle_write_rows_event(event, catalog_entry, state, columns, rows_saved, time_extracted, bookmark):
    stream_version = common.get_stream_version(catalog_entry.tap_stream_id, state)
    db_column_types = get_db_column_types(event)

    line_number = 0
    for row in event.rows:
        line_number += 1
        event_ts = datetime.datetime.utcfromtimestamp(event.timestamp).replace(tzinfo=pytz.UTC)
        vals = row['values']
        vals[SYS_UPDATED_AT] = event_ts
        vals[SYS_EVENT_TYPE] = INSERT_EVENT
        vals[SYS_LOG_POS] = bookmark.get('log_pos', -1)
        vals[SYS_LOG_FILE] = int(bookmark.get('log_file', 'mysql-bin-changelog.-1').split('.')[-1])
        vals[SYS_LINENO] = line_number

        filtered_vals = {k: v for k, v in vals.items()
                         if k in columns}

        record_message = row_to_singer_record(catalog_entry,
                                              stream_version,
                                              db_column_types,
                                              filtered_vals,
                                              time_extracted)

        singer.write_message(record_message)
        rows_saved = rows_saved + 1

    return rows_saved


def handle_update_rows_event(event, catalog_entry, state, columns, rows_saved, time_extracted, bookmark):
    stream_version = common.get_stream_version(catalog_entry.tap_stream_id, state)
    db_column_types = get_db_column_types(event)

    line_number = 0
    for row in event.rows:
        line_number += 1
        event_ts = datetime.datetime.utcfromtimestamp(event.timestamp).replace(tzinfo=pytz.UTC)
        vals = row['after_values']
        vals[SYS_UPDATED_AT] = event_ts
        vals[SYS_EVENT_TYPE] = UPDATE_EVENT
        vals[SYS_LOG_POS] = bookmark.get('log_pos', -1)
        vals[SYS_LOG_FILE] = int(bookmark.get('log_file', 'mysql-bin-changelog.-1').split('.')[-1])
        vals[SYS_LINENO] = line_number


        filtered_vals = {k: v for k, v in vals.items()
                         if k in columns}

        record_message = row_to_singer_record(catalog_entry,
                                              stream_version,
                                              db_column_types,
                                              filtered_vals,
                                              time_extracted)

        singer.write_message(record_message)

        rows_saved = rows_saved + 1

    return rows_saved


def handle_delete_rows_event(event, catalog_entry, state, columns, rows_saved, time_extracted, bookmark):
    stream_version = common.get_stream_version(catalog_entry.tap_stream_id, state)
    db_column_types = get_db_column_types(event)

    line_number = 0
    for row in event.rows:
        line_number += 1
        event_ts = datetime.datetime.utcfromtimestamp(event.timestamp).replace(tzinfo=pytz.UTC)

        vals = row['values']

        vals[SDC_DELETED_AT] = event_ts
        vals[SYS_UPDATED_AT] = event_ts
        vals[SYS_EVENT_TYPE] = DELETE_EVENT
        vals[SYS_LOG_POS] = bookmark.get('log_pos', -1)
        vals[SYS_LOG_FILE] = int(bookmark.get('log_file', 'mysql-bin-changelog.-1').split('.')[-1])
        vals[SYS_LINENO] = line_number

        filtered_vals = {k: v for k, v in vals.items()
                         if k in columns}

        record_message = row_to_singer_record(catalog_entry,
                                              stream_version,
                                              db_column_types,
                                              filtered_vals,
                                              time_extracted)

        singer.write_message(record_message)

        rows_saved = rows_saved + 1

    return rows_saved


def generate_streams_map(binlog_streams):
    stream_map = {}

    for catalog_entry in binlog_streams:
        columns = add_automatic_properties(catalog_entry,
                                           list(catalog_entry.schema.properties.keys()))

        stream_map[catalog_entry.tap_stream_id] = {
            'catalog_entry': catalog_entry,
            'desired_columns': columns
        }

    return stream_map


def _run_binlog_sync(mysql_conn, reader, binlog_streams_map, state, config: Dict):
    time_extracted = utils.now()

    rows_saved = 0
    events_skipped = 0

    current_log_file, current_log_pos = fetch_current_log_file_and_pos(mysql_conn)
    current_state = {
            'log_file': None,
            'log_pos': None,
            'timestamp': 0
    }

    last_binlog_event = None
    for binlog_event in reader:

        last_binlog_event = binlog_event
        current_state['timestamp'] = max(current_state['timestamp'], binlog_event.timestamp)

        if isinstance(binlog_event, RotateEvent):
            next_state = {
                    'log_file': binlog_event.next_binlog,
                    'log_pos': binlog_event.position,
                    'timestamp': max(current_state['timestamp'], 0)
            }

            LOGGER.info("LOG Rotated: {}".format(next_state['log_file']))
            state = update_bookmarks(state,
                                     binlog_streams_map,
                                     next_state)
        else:
            tap_stream_id = common.generate_tap_stream_id(binlog_event.schema, binlog_event.table)
            streams_map_entry = binlog_streams_map.get(tap_stream_id, {})
            catalog_entry = streams_map_entry.get('catalog_entry')
            columns = streams_map_entry.get('desired_columns')

            if not catalog_entry:
                events_skipped = events_skipped + 1

                if events_skipped % UPDATE_BOOKMARK_PERIOD == 0:
                    LOGGER.debug("Skipped %s events so far as they were not for selected tables; %s rows extracted",
                                 events_skipped,
                                 rows_saved)

            else:

                # Compare event's columns to the schema properties
                # if a column no longer exists, the event will have something like __dropped_col_XY__
                # to refer to this column, we don't want these columns to be included in the difference
                diff = set(filter(lambda k: False if re.match(r'__dropped_col_\d+__', k) else True,
                                  get_db_column_types(binlog_event).keys())).\
                    difference(catalog_entry.schema.properties.keys())

                # If there are additional cols in the event then run discovery and update the catalog
                if diff:
                    LOGGER.debug('Difference between event and schema: %s', diff)
                    LOGGER.info('Running discovery ... ')

                    # run discovery for the current table only
                    new_catalog_entry = discover_catalog(mysql_conn,
                                                     config.get('filter_dbs'),
                                                     catalog_entry.table).streams[0]

                    selected = {k for k, v in new_catalog_entry.schema.properties.items()
                                if common.property_is_selected(new_catalog_entry, k)}

                    # the new catalog has "stream" property = table name, we need to update that to make it the same as
                    # the result of the "resolve_catalog" function
                    new_catalog_entry.stream = tap_stream_id

                    # These are the columns we need to select
                    new_columns = desired_columns(selected, new_catalog_entry.schema)

                    cols = set(new_catalog_entry.schema.properties.keys())

                    # drop unsupported properties from schema
                    for col in cols:
                        if col not in new_columns:
                            new_catalog_entry.schema.properties.pop(col, None)

                    # Add the _sdc_deleted_at col
                    new_columns = add_automatic_properties(new_catalog_entry, list(new_columns))

                    # send the new scheme to target if we have a new schema
                    if new_catalog_entry.schema.properties != catalog_entry.schema.properties:
                        write_schema_message(catalog_entry=new_catalog_entry)
                        catalog_entry = new_catalog_entry

                        # update this dictionary while we're at it
                        binlog_streams_map[tap_stream_id]['catalog_entry'] = new_catalog_entry
                        binlog_streams_map[tap_stream_id]['desired_columns'] = new_columns
                        columns = new_columns

                bookmark = {
                        'log_pos': reader.log_pos,
                        'log_file': reader.log_file
                }
                if isinstance(binlog_event, WriteRowsEvent):
                    rows_saved = handle_write_rows_event(binlog_event,
                                                         catalog_entry,
                                                         state,
                                                         columns,
                                                         rows_saved,
                                                         time_extracted,
                                                         bookmark)

                elif isinstance(binlog_event, UpdateRowsEvent):
                    rows_saved = handle_update_rows_event(binlog_event,
                                                          catalog_entry,
                                                          state,
                                                          columns,
                                                          rows_saved,
                                                          time_extracted,
                                                          bookmark)

                elif isinstance(binlog_event, DeleteRowsEvent):
                    rows_saved = handle_delete_rows_event(binlog_event,
                                                          catalog_entry,
                                                          state,
                                                          columns,
                                                          rows_saved,
                                                          time_extracted,
                                                          bookmark)
                else:
                    LOGGER.debug("Skipping event for table %s.%s as it is not an INSERT, UPDATE, or DELETE",
                                 binlog_event.schema,
                                 binlog_event.table)

        # Update log_file and log_pos after every processed binlog event
        current_state['log_file'] = reader.log_file
        current_state['log_pos'] = reader.log_pos

        # The iterator across python-mysql-replication's fetchone method should ultimately terminate
        # upon receiving an EOF packet. There seem to be some cases when a MySQL server will not send
        # one causing binlog replication to hang.
        if current_log_file == current_state['log_file'] and current_state['log_pos'] >= current_log_pos:
            LOGGER.info("BREAKING {} : {}".format(current_log_file, current_log_pos))
            break

        # Update singer bookmark and send STATE message periodically
        if ((rows_saved and rows_saved % UPDATE_BOOKMARK_PERIOD == 0) or
                (events_skipped and events_skipped % UPDATE_BOOKMARK_PERIOD == 0)):
            state = update_bookmarks(state,
                                     binlog_streams_map,
                                     current_state)
            singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    LOGGER.info("ALL DONE SOMEHOW")

    # Update singer bookmark at the last time to point it the the last processed binlog event
    if current_state['log_pos'] and current_state['log_file']:
        state = update_bookmarks(state,
                                 binlog_streams_map,
                                 current_state)


def sync_binlog_stream(mysql_conn, config, binlog_streams, state):
    binlog_streams_map = generate_streams_map(binlog_streams)

    for tap_stream_id, _ in binlog_streams_map.items():
        common.whitelist_bookmark_keys(BOOKMARK_KEYS, tap_stream_id, state)

    log_file, log_pos = calculate_bookmark(mysql_conn, binlog_streams_map, state)

    verify_log_file_exists(mysql_conn, log_file, log_pos)

    if config.get('server_id'):
        server_id = int(config.get('server_id'))
        LOGGER.info("Using provided server_id=%s", server_id)
    else:
        server_id = fetch_server_id(mysql_conn)
        LOGGER.info("No server_id provided, will use global server_id=%s", server_id)

    connection_wrapper = make_connection_wrapper(config)
    reader = None

    try:
        slave_uuid = f"bi-reader-%04x" % random.getrandbits(64)

        reader = BinLogStreamReader(
            connection_settings={},
            server_id=server_id,
            slave_uuid=slave_uuid,
            log_file=log_file,
            log_pos=log_pos,
            resume_stream=True,
            only_events=[RotateEvent, WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent],
            pymysql_wrapper=connection_wrapper,
        )
        LOGGER.info("Starting binlog replication with log_file=%s, log_pos=%s", log_file, log_pos)
        _run_binlog_sync(mysql_conn, reader, binlog_streams_map, state, config)
    finally:
        # BinLogStreamReader doesn't implement the `with` methods
        # So, try/finally will close the chain from the top
        reader.close()

    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
