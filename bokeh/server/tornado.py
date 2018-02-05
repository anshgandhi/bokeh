''' Provides the Bokeh Server Tornado application.

'''
from __future__ import absolute_import, print_function

import logging
log = logging.getLogger(__name__)

import os
from pprint import pformat

from tornado import gen
from tornado.ioloop import PeriodicCallback
from tornado.web import Application as TornadoApplication
from tornado.web import StaticFileHandler

from ..application import Application
from ..resources import Resources
from ..settings import settings

from .application_context import ApplicationContext
from .connection import ServerConnection
from .urls import per_app_patterns, toplevel_patterns
from .views.root_handler import RootHandler
from .views.static_handler import StaticHandler

class BokehTornado(TornadoApplication):
    ''' A Tornado Application used to implement the Bokeh Server.

    Args:
        applications (dict[str,Application] or Application) :
            A map from paths to ``Application`` instances.

            If the value is a single Application, then the following mapping
            is generated:

            .. code-block:: python

                applications = { '/' : applications }

            When a connection comes in to a given path, the associate
            Application is used to generate a new document for the session.

        prefix (str, optional) :
            A URL prefix to use for all Bokeh server paths. (default: None)

        extra_websocket_origins (list[str], optional) :
            A list of hosts that can connect to the websocket.

            This is typically required when embedding a Bokeh server app in an
            external web site using :func:`~bokeh.embed.server_document` or
            similar.

            If None, ``["localhost"]`` will be assumed (default: None)

        extra_patterns (seq[tuple], optional) :
            A list of tuples of (str, http or websocket handler)

            Use this argument to add additional endpoints to custom deployments
            of the Bokeh Server.

            If None, then ``[]`` will be used. (default: None)

        secret_key (str, optional) :
            A secret key for signing session IDs.

            Defaults to the current value of the environment variable
            ``BOKEH_SECRET_KEY``

        sign_sessions (bool, optional) :
            Whether to cryptographically sign session IDs

            Defaults to the current value of the environment variable
            ``BOKEH_SIGN_SESSIONS``. If ``True``, then ``secret_key`` must
            also be provided (either via environment setting or passed as
            a parameter value)

        generate_session_ids (bool, optional) :
            Whether to generate a session ID if one is not provided
            (default: True)

        keep_alive_milliseconds (int, optional) :
            Number of milliseconds between keep-alive pings (default: 37000)

            Pings normally required to keep the websocket open. Set to 0 to
            disable pings.

        check_unused_sessions_milliseconds (int, optional) :
            Number of milliseconds between checking for unused sessions
            (default: 17000)

        unused_session_lifetime_milliseconds (int, optional) :
            Number of milliseconds for unused session lifetime (default: 15000)

        stats_log_frequency_milliseconds (int, optional) :
            Number of milliseconds between logging stats (default: 15000)

        use_index (bool, optional) :
            Whether to generate an index of running apps in the ``RootHandler``
            (default: True)

        redirect_root (bool, optional) :
            When there is only a single running application, whether to
            redirect requests to ``"/"`` to that application automatically
            (default: True)

            If there are multiple Bokeh applications configured, this option
            has no effect.

    '''

    def __init__(self,
                 applications,
                 prefix=None,
                 extra_websocket_origins=None,
                 extra_patterns=None,
                 secret_key=settings.secret_key_bytes(),
                 sign_sessions=settings.sign_sessions(),
                 generate_session_ids=True,
                 keep_alive_milliseconds=37000, # heroku, nginx default to 60s timeout, so use less than that
                 check_unused_sessions_milliseconds=17000,
                 unused_session_lifetime_milliseconds=15000,
                 stats_log_frequency_milliseconds=15000,
                 use_index=True,
                 redirect_root=True):

        # This will be set when initialize is called
        self._loop = None

        if isinstance(applications, Application):
            applications = { '/' : applications }

        if prefix is None:
            prefix = ""
        prefix = prefix.strip("/")
        if prefix:
            prefix = "/" + prefix

        self._prefix = prefix

        if keep_alive_milliseconds < 0:
            # 0 means "disable"
            raise ValueError("keep_alive_milliseconds must be >= 0")

        if check_unused_sessions_milliseconds <= 0:
            raise ValueError("check_unused_sessions_milliseconds must be > 0")

        if unused_session_lifetime_milliseconds <= 0:
            raise ValueError("check_unused_sessions_milliseconds must be > 0")

        if stats_log_frequency_milliseconds <= 0:
            raise ValueError("stats_log_frequency_milliseconds must be > 0")

        self._keep_alive_milliseconds = keep_alive_milliseconds
        self._check_unused_sessions_milliseconds = check_unused_sessions_milliseconds
        self._unused_session_lifetime_milliseconds = unused_session_lifetime_milliseconds
        self._stats_log_frequency_milliseconds = stats_log_frequency_milliseconds

        if extra_websocket_origins is None:
            self._websocket_origins = set()
        else:
            self._websocket_origins = set(extra_websocket_origins)
        self._secret_key = secret_key
        self._sign_sessions = sign_sessions
        self._generate_session_ids = generate_session_ids
        log.debug("These host origins can connect to the websocket: %r", list(self._websocket_origins))

        # Wrap applications in ApplicationContext
        self._applications = dict()
        for k,v in applications.items():
            self._applications[k] = ApplicationContext(v,url=k)

        extra_patterns = extra_patterns or []
        all_patterns = []
        for key, app in applications.items():
            app_patterns = []
            for p in per_app_patterns:
                if key == "/":
                    route = p[0]
                else:
                    route = key + p[0]
                route = self._prefix + route
                app_patterns.append((route, p[1], { "application_context" : self._applications[key] }))

            websocket_path = None
            for r in app_patterns:
                if r[0].endswith("/ws"):
                    websocket_path = r[0]
            if not websocket_path:
                raise RuntimeError("Couldn't find websocket path")
            for r in app_patterns:
                r[2]["bokeh_websocket_path"] = websocket_path

            all_patterns.extend(app_patterns)

            # add a per-app static path if requested by the application
            if app.static_path is not None:
                if key == "/":
                    route = "/static/(.*)"
                else:
                    route = key + "/static/(.*)"
                route = self._prefix + route
                all_patterns.append((route, StaticFileHandler, { "path" : app.static_path }))

        for p in extra_patterns + toplevel_patterns:
            if p[1] == RootHandler:
                if use_index:
                    data = {"applications": self._applications,
                            "prefix": self._prefix,
                            "use_redirect": redirect_root}
                    prefixed_pat = (self._prefix + p[0],) + p[1:] + (data,)
                    all_patterns.append(prefixed_pat)
            else:
                prefixed_pat = (self._prefix + p[0],) + p[1:]
                all_patterns.append(prefixed_pat)

        log.debug("Patterns are:")
        for line in pformat(all_patterns, width=60).split("\n"):
            log.debug("  " + line)

        super(BokehTornado, self).__init__(all_patterns)

    def initialize(self, io_loop):
        ''' Start a Bokeh Server Tornado Application on a given Tornado IOLoop.

        '''
        self._loop = io_loop

        for app_context in self._applications.values():
            app_context._loop = self._loop

        self._clients = set()

        self._stats_job = PeriodicCallback(self._log_stats,
                                           self._stats_log_frequency_milliseconds)

        self._cleanup_job = PeriodicCallback(self._cleanup_sessions,
                                             self._check_unused_sessions_milliseconds)

        if self._keep_alive_milliseconds > 0:
            self._ping_job = PeriodicCallback(self._keep_alive, self._keep_alive_milliseconds)
        else:
            self._ping_job = None

    @property
    def app_paths(self):
        ''' A list of all application paths for all Bokeh applications
        configured on this Bokeh server instance.

        '''
        return set(self._applications)

    @property
    def io_loop(self):
        ''' The Tornado IOLoop that this  Bokeh Server Tornado Application
        is running on.

        '''
        return self._loop

    @property
    def prefix(self):
        ''' A URL prefix for this Bokeh Server Tornado Application to use
        for all paths

        '''
        return self._prefix

    @property
    def websocket_origins(self):
        ''' A list of websocket origins permitted to connect to this server.

        '''
        return self._websocket_origins

    @property
    def secret_key(self):
        ''' A secret key for this Bokeh Server Tornado Application to use when
        signing session IDs, if configured.

        '''
        return self._secret_key

    @property
    def sign_sessions(self):
        ''' Whether this Bokeh Server Tornado Application has been configured
        to cryptographically sign session IDs

        If ``True``, then ``secret_key`` must also have been configured.
        '''
        return self._sign_sessions

    @property
    def generate_session_ids(self):
        ''' Whether this Bokeh Server Tornado Application has been configured
        to automatically generate session IDs.

        '''
        return self._generate_session_ids

    def resources(self, absolute_url=None):
        ''' Provide a :class:`~bokeh.resources.Resources` that specifies where
        Bokeh application sessions should load BokehJS resources from.

        Args:
            absolute_url (bool):
                An absolute URL prefix to use for locating resources. If None,
                relative URLs are used (default: None)

        '''
        if absolute_url:
            return Resources(mode="server", root_url=absolute_url + self._prefix, path_versioner=StaticHandler.append_version)
        return Resources(mode="server", root_url=self._prefix, path_versioner=StaticHandler.append_version)

    def start(self):
        ''' Start the Bokeh Server application.

        Starting the Bokeh Server Tornado application will run periodic
        callbacks for stats logging, cleanup, pinging, etc. Additionally, any
        startup hooks defined by the configured Bokeh applications will be run.

        '''
        self._stats_job.start()
        self._cleanup_job.start()
        if self._ping_job is not None:
            self._ping_job.start()

        for context in self._applications.values():
            context.run_load_hook()

    def stop(self, wait=True):
        ''' Stop the Bokeh Server application.

        Args:
            wait (bool): whether to wait for orderly cleanup (default: True)

        Returns:
            None

        '''

        # TODO should probably close all connections and shut down all sessions here
        for context in self._applications.values():
            context.run_unload_hook()

        self._stats_job.stop()
        self._cleanup_job.stop()
        if self._ping_job is not None:
            self._ping_job.stop()

        self._clients.clear()

    def new_connection(self, protocol, socket, application_context, session):
        connection = ServerConnection(protocol, socket, application_context, session)
        self._clients.add(connection)
        return connection

    def client_lost(self, connection):
        self._clients.discard(connection)
        connection.detach_session()

    def get_session(self, app_path, session_id):
        ''' Get an active a session by name application path and session ID.

        Args:
            app_path (str) :
                The configured application path for the application to return
                a session for.

            session_id (str) :
                The session ID of the session to retrieve.

        Returns:
            ServerSession

        '''
        if app_path not in self._applications:
            raise ValueError("Application %s does not exist on this server" % app_path)
        return self._applications[app_path].get_session(session_id)

    def get_sessions(self, app_path):
        ''' Gets all currently active sessions for an application.

        Args:
            app_path (str) :
                The configured application path for the application to return
                sessions for.

        Returns:
            list[ServerSession]

        '''
        if app_path not in self._applications:
            raise ValueError("Application %s does not exist on this server" % app_path)
        return list(self._applications[app_path].sessions)

    # Periodic Callbacks ------------------------------------------------------

    @gen.coroutine
    def _cleanup_sessions(self):
        log.trace("Running session cleanup job")
        for app in self._applications.values():
            yield app._cleanup_sessions(self._unused_session_lifetime_milliseconds)
        raise gen.Return(None)

    def _log_stats(self):
        if log.getEffectiveLevel() > logging.DEBUG:
            # avoid the work below if we aren't going to log anything
            return
        log.debug("[pid %d] %d clients connected", os.getpid(), len(self._clients))
        for app_path, app in self._applications.items():
            sessions = list(app.sessions)
            unused_count = 0
            for s in sessions:
                if s.connection_count == 0:
                    unused_count += 1
            log.debug("[pid %d]   %s has %d sessions with %d unused",
                      os.getpid(), app_path, len(sessions), unused_count)

    def _keep_alive(self):
        for c in self._clients:
            c.send_ping()
