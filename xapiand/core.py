from __future__ import absolute_import, unicode_literals

import re

from hashlib import md5

import xapian

from .exceptions import InvalidIndexError
from .serialise import serialise_value, normalize

KEY_RE = re.compile(r'[_a-zA-Z][_a-zA-Z0-9]*')


def _xapian_database(databases_pool, endpoints, writable, data='.', log=None):
    if endpoints in databases_pool:
        database = databases_pool[endpoints]
    else:
        if writable:
            database = xapian.WritableDatabase()
        else:
            database = xapian.Database()
        databases = len(endpoints)
        database._all_databases = [None] * databases
        database._all_databases_config = [None] * databases
        database._endpoints = endpoints
        database._databases_pool = databases_pool
        databases_pool[endpoints] = database
    for subdatabase_number, db in enumerate(endpoints):
        if database._all_databases[subdatabase_number] is None:
            _database = _xapian_subdatabase(databases_pool, db, writable, data, log)
            database._all_databases[subdatabase_number] = _database
            database._all_databases_config[subdatabase_number] = (_xapian_subdatabase, (databases_pool, db, writable, data, log))
            if _database:
                database.add_database(_database)
    return database


def _xapian_subdatabase(databases_pool, db, writable, data='.', log=None):
    if isinstance(db, basestring):
        path = db
        return _xapian_database_open(databases_pool, path, writable, data, log)
    else:
        host, port, timeout = db
        return _xapian_database_connect(databases_pool, host, port, timeout, writable, data, log)


def _xapian_database_open(databases_pool, path, writable, data='.', log=None):
    key = path
    if key in databases_pool:
        return databases_pool[key]
    try:
        if writable:
            database = xapian.WritableDatabase(path, xapian.DB_CREATE_OR_OPEN)
        else:
            try:
                database = xapian.Database(path)
            except xapian.DatabaseError:
                database = xapian.WritableDatabase(path, xapian.DB_CREATE_OR_OPEN)
                database.close()
                database = xapian.Database(path)
    except xapian.DatabaseLockError:
        raise InvalidIndexError(u'Unable to lock index at %s' % path)
    except xapian.DatabaseOpeningError:
        raise InvalidIndexError(u'Unable to open index at %s' % path)
    except xapian.DatabaseError:
        raise InvalidIndexError(u'Unable to use index at %s' % path)
    databases_pool[key] = database
    return database


def _xapian_database_connect(databases_pool, host, port, timeout, writable, data='.', log=None):
    key = (host, port,)
    if key in databases_pool:
        return databases_pool[key]
    try:
        if writable:
            database = xapian.remote_open_writable(host, port, timeout)
        else:
            database = xapian.remote_open(host, port, timeout)
    except xapian.NetworkError:
        raise InvalidIndexError(u'Unable to connect to index at %s:%s' % (host, port))
    databases_pool[key] = database
    return database


def xapian_endpoints(paths, locations, timeout):
    endpoints = []
    for path in paths:
        endpoints.append(path)
    for host, port in locations:
        endpoints.append((host, port, timeout))
    return tuple(endpoints)


def xapian_database(databases_pool, endpoints, writable, data='.', log=None):
    """
    Returns a xapian.Database with multiple endpoints attached.

    """
    database = _xapian_database(databases_pool, endpoints, writable, data=data, log=log)
    if not writable:  # Make sure we always read the latest:
        database = xapian_reopen(database, data=data, log=log)
    return database


def xapian_reopen(database, data='.', log=None):
    try:
        database.reopen()
    except (xapian.DatabaseOpeningError, xapian.NetworkError):
        # Could not be opened, try full reopen:
        _all_databases = database._all_databases
        _all_databases_config = database._all_databases_config
        _endpoints = database._endpoints
        _databases_pool = database._databases_pool
        _writable = isinstance(database, xapian.WritableDatabase)

        del _databases_pool[_endpoints]
        database = _xapian_database(_databases_pool, _endpoints, _writable, data=data, log=log)

        # Recover subdatabases:
        database._all_databases = _all_databases
        database._all_databases_config = _all_databases_config
        database._endpoints = _endpoints
        database._databases_pool = _databases_pool
        for subdatabase_number, db in enumerate(_endpoints):
            _database = database._all_databases[subdatabase_number]
            if _database:
                database.add_database(_database)

    for subdatabase in database._all_databases:
        if subdatabase:
            try:
                subdatabase.reopen()
            except (xapian.DatabaseOpeningError, xapian.NetworkError):
                subdatabase_number = database._all_databases.index(subdatabase)
                _database_fn, args = database._all_databases_config[subdatabase_number]
                subdatabase = _database_fn(*args)
                database._all_databases[subdatabase_number] = subdatabase

    return database


def xapian_index(databases_pool, db, document, commit=False, data='.', log=None):
    subdatabase = _xapian_subdatabase(databases_pool, db, True, data, log)
    if not subdatabase:
        log.error("Database is None (db:%s)", db)
        return

    document_id, document_values, document_terms, document_texts, document_data, default_language, default_spelling, default_positions = document

    document = xapian.Document()

    for name, value in (document_values or {}).items():
        name = name.strip().lower()
        if KEY_RE.match(name):
            slot = int(md5(name.lower()).hexdigest(), 16) & 0xffffffff
            value = serialise_value(value)
            document.add_value(slot, value)
        else:
            log.warning("Ignored document value name (%r)", name)

    if isinstance(document_id, basestring):
        document.add_boolean_term(document_id)  # Make sure document_id is also a term (otherwise it doesn't replace an existing document)

    for term in document_terms or ():
        if isinstance(term, (tuple, list)):
            term, weight, prefix, position = (list(term) + [None] * 4)[:4]
        else:
            weight = prefix = position = None
        if not term:
            continue

        weight = 1 if weight is None else weight
        prefix = '' if prefix is None else prefix

        term = normalize(serialise_value(term))
        if position is None:
            document.add_term(prefix + term, weight)
        else:
            document.add_posting(prefix + term, position, weight)

    for text in document_texts or ():
        if isinstance(text, (tuple, list)):
            text, weight, prefix, language, spelling, positions = (list(text) + [None] * 6)[:6]
        else:
            weight = prefix = language = spelling = positions = None
        if not text:
            continue

        weight = 1 if weight is None else weight
        prefix = '' if prefix is None else prefix
        language = default_language if language is None else language
        positions = default_positions if positions is None else positions
        spelling = default_spelling if spelling is None else spelling

        term_generator = xapian.TermGenerator()
        term_generator.set_database(subdatabase)
        term_generator.set_document(document)
        if language:
            term_generator.set_stemmer(xapian.Stem(language))
        if positions:
            index_text = term_generator.index_text
        else:
            index_text = term_generator.index_text_without_positions
        if spelling:
            term_generator.set_flags(xapian.TermGenerator.FLAG_SPELLING)
        index_text(normalize(text), weight, prefix)

    if document_data:
        document.set_data(document_data)

    try:
        docid = subdatabase.replace_document(document_id, document)
    except xapian.InvalidArgumentError as e:
        log.error(e, exc_info=True)

    if commit:
        subdatabase.commit()

    return docid


def xapian_delete(databases_pool, db, document_id, commit=False, data='.', log=None):
    subdatabase = _xapian_subdatabase(databases_pool, db, True, data=data, log=log)
    if not subdatabase:
        log.error("Database is None (db:%s)", db)
        return

    subdatabase.delete_document(document_id)

    if commit:
        subdatabase.commit()


def xapian_commit(databases_pool, db, data='.', log=None):
    subdatabase = _xapian_subdatabase(databases_pool, db, True, data=data, log=log)
    if not subdatabase:
        log.error("Database is None (db:%s)", db)
        return

    subdatabase.commit()
