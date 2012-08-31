import logging
import pylons
import sqlalchemy
from sqlalchemy.exc import ProgrammingError
import ckan.plugins as p
import ckanext.datastore.logic.action as action
import ckanext.datastore.logic.auth as auth
import ckanext.datastore.db as db
import ckan.logic as logic

log = logging.getLogger(__name__)
_get_or_bust = logic.get_or_bust


class DatastoreException(Exception):
    pass


class DatastorePlugin(p.SingletonPlugin):
    '''
    Datastore plugin.
    '''
    p.implements(p.IConfigurable, inherit=True)
    p.implements(p.IActions)
    p.implements(p.IAuthFunctions)

    def configure(self, config):
        self.config = config
        # check for ckan.datastore_write_url
        if (not 'ckan.datastore_write_url' in config):
            error_msg = 'ckan.datastore_write_url not found in config'
            raise DatastoreException(error_msg)

        ## Do light wrapping around action function to add datastore_active
        ## to resource dict.  Not using IAction extension as this prevents other plugins
        ## from having a custom resource_read.

        if not config['debug'] and 'ckan.datastore_read_url':
            self.ckan_url = self.config['sqlalchemy.url']
            self.write_url = self.config['ckan.datastore_write_url']
            self.read_url = self.config['ckan.datastore_read_url']

            self._check_separate_db()
            self._check_read_permissions()

        # Make sure actions are cached
        resource_show = p.toolkit.get_action('resource_show')

        # TODO: move to package.py or better: have a think about it
        def new_resource_show(context, data_dict):
            engine = db._get_engine(
                context,
                {'connection_url': config['ckan.datastore_write_url']}
            )
            new_data_dict = resource_show(context, data_dict)
            try:
                connection = engine.connect()
                result = connection.execute(
                    'select 1 from pg_tables where tablename = %s',
                    new_data_dict['id']
                ).fetchone()
                if result:
                    new_data_dict['datastore_active'] = True
                else:
                    new_data_dict['datastore_active'] = False
            finally:
                connection.close()
            return new_data_dict

        self._create_alias_table()

        ## Make sure do not run many times if configure is called repeatedly
        ## as in tests.
        if not hasattr(resource_show, '_datastore_wrapped'):
            new_resource_show._datastore_wrapped = True
            logic._actions['resource_show'] = new_resource_show

    def _check_separate_db(self):
        '''
        Make sure the datastore is on a separate db. Otherwise one could access
        all internal tables via the api.
        '''

        if  self.write_url == self.read_url:
            raise Exception("The write and read-only database connection url are the same.")

        if self._get_db_from_url(self.ckan_url) == self._get_db_from_url(self.read_url):
            raise Exception("The CKAN and datastore database are the same.")

    def _get_db_from_url(self, url):
        return url[url.rindex("@"):]

    def _check_read_permissions(self):
        '''
        Check whether the right permissions are set for the read only user.
        A table is created by the write user to test the read only user.
        '''
        write_connection = db._get_engine(None,
            {'connection_url': self.write_url}).connect()
        write_connection.execute(u"CREATE TABLE public.foo (id INTEGER NOT NULL, name VARCHAR)")

        read_connection = db._get_engine(None,
            {'connection_url': self.read_url}).connect()
        read_trans = read_connection.begin()

        statements = [
            u"CREATE TABLE public.bar (id INTEGER NOT NULL, name VARCHAR)",
            u"INSERT INTO public.foo VALUES (1, 'okfn')"
        ]

        try:
            for sql in statements:
                read_trans = read_connection.begin()
                try:
                    read_connection.execute(sql)
                except ProgrammingError, e:
                    if 'permission denied' not in str(e):
                        raise
                else:
                    log.info("Connection url {}"
                        .format(self.read_url))
                    raise Exception("We have write permissions on the read-only database.")
                finally:
                    read_trans.rollback()
        except Exception:
            raise
        finally:
            write_connection.execute("DROP TABLE foo")

    def _create_alias_table(self):
        mapping_sql = '''
            SELECT distinct
                d.refobjid::regclass AS main,
                r.ev_class::regclass AS alias
            FROM
                pg_attribute    as a
                JOIN pg_depend  as d on d.refobjid = a.attrelid AND d.refobjsubid = a.attnum
                JOIN pg_rewrite as r ON d.objid = r.oid
                JOIN pg_class as dependee ON r.ev_class = dependee.oid
                JOIN pg_class as dependent ON d.refobjid = dependent.oid
            WHERE dependee.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
        '''
        create_alias_table_sql = u'create or replace view alias_mapping as {}'.format(mapping_sql)
        connection = db._get_engine(None,
            {'connection_url': pylons.config['ckan.datastore_write_url']}).connect()
        connection.execute(create_alias_table_sql)

    def get_actions(self):
        available_actions = {'datastore_create': action.datastore_create,
                'datastore_delete': action.datastore_delete,
                'datastore_search': action.datastore_search}
        if 'ckan.datastore_read_url' in self.config:
            available_actions['data_search_sql'] = action.data_search_sql
        return available_actions

    def get_auth_functions(self):
        return {'datastore_create': auth.datastore_create,
                'datastore_delete': auth.datastore_delete,
                'datastore_search': auth.datastore_search}