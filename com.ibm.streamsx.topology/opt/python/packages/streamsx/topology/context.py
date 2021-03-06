# coding=utf-8
# Licensed Materials - Property of IBM
# Copyright IBM Corp. 2016,2017
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
try:
    from future import standard_library
    standard_library.install_aliases()
except (ImportError, NameError):
    # nothing to do here
    pass
# Licensed Materials - Property of IBM
# Copyright IBM Corp. 2015

from streamsx import rest
import logging
import tempfile
import os
import os.path
import json
import subprocess
import threading
import sys
import enum
import codecs
import tempfile

logger = logging.getLogger('streamsx.topology.context')

#
# Submission of a python graph using the Java Application API
# The JAA is reused to have a single set of code_createJSONFile that creates
# SPL, the toolkit, the bundle and submits it to the relevant
# environment
#
def submit(ctxtype, graph, config=None, username=None, password=None):
    """
    Submits a topology with the specified context type.

    Args:
        ctxtype (string): context type.  Values include:
        * DISTRIBUTED - the topology is submitted to a Streams instance.
          The bundle is submitted using `streamtool` which must be setup to submit without requiring authentication
          input. Additionally, a username and password may optionally be provided to enable retrieving data from remote
          views.
        * STANDALONE - the topology is executed directly as an Streams standalone application.
          The standalone execution is spawned as a separate process
        * BUNDLE - execution of the topology produces an SPL application bundle
          (.sab file) that can be submitted to an IBM Streams instance as a distributed application.
        * JUPYTER - the topology is run in standalone mode, and context.submit returns a stdout streams of bytes which
          can be read from to visualize the output of the application.
        * BUILD_ARCHIVE - Creates a Bluemix-compatible build archive.
          execution of the topology produces a build archive, which can be submitted to a streaming
          analytics Bluemix remote build service.
        * ANALYTICS_SERVICE - If a local IBM Streams install is present, the application is built locally and then submitted
          to an IBM Bluemix Streaming Analytics service. If a local IBM Streams install is not present, the application is
          submitted to, built, and executed on an IBM Bluemix Streaming Analytics service. If the ConfigParams.FORCE_REMOTE_BUILD
          flag is set to True, the application will be built by the service even if a local Streams install is present.
          The service is described by its VCAP services and a service name pointing to an instance within the VCAP services. The VCAP services is either set in the configuration object or as the environment variable VCAP_SERVICES.
        graph: a Topology object.
        config (dict): a configuration object containing job configurations and/or submission information. Keys include:
        * ConfigParams.VCAP_SERVICES ('topology.service.vcap') - VCAP services information for the ANALYTICS_SERVICE context. Supported formats are a dict obtained from the JSON VCAP services, a string containing the serialized JSON form or a file name pointing to a file containing the JSON form.
        * ConfigParams.SERVICE_NAME ('topology.service.name') - the name of the Streaming Analytics service for submission.
        * ConfigParams.FORCE_REMOTE_BUILD ('topology.forceRemoteBuild') - A flag which will force the application to be compiled and submitted remotely, if possible.
        username (string): an optional SWS username. Needed for retrieving remote view data.
        password (string): an optional SWS password. Used in conjunction with the username, and needed for retrieving
        remote view data.
        log_level: The maximum logging level for log output.

    Returns:
        An output stream of bytes if submitting with JUPYTER, otherwise returns a dict containing information relevant
        to the submission.
    """

    graph = graph.graph

    if not graph.operators:
        raise ValueError("Topology {0} does not contain any streams.".format(graph.topology.name))

    context_submitter = _SubmitContextFactory(graph, config, username, password).get_submit_context(ctxtype)
    try:
        return context_submitter.submit()
    except:
        logger.exception("Error while submitting application.")


class _BaseSubmitter(object):
    """
    A submitter which handles submit operations common across all submitter types..
    """
    def __init__(self, ctxtype, config, graph):
        self.ctxtype = ctxtype
        self.config = dict()
        if config is not None:
            # Make copy of config to avoid modifying
            # the callers config
            self.config.update(config)
        self.graph = graph
        self.fn = None
        self.results_file = None

    def _config(self):
        "Return the submit configuration"
        return self.config

    def submit(self):
        # Convert the JobConfig into overlays
        self._create_job_config_overlays()

        # encode the relevant python version information into the config
        self._add_python_info()

        # Create the json file containing the representation of the application
        self._create_json_file(self._create_full_json())

        tk_root = self._get_toolkit_root()

        cp = os.path.join(tk_root, "lib", "com.ibm.streamsx.topology.jar")

        streams_install = os.environ.get('STREAMS_INSTALL')
        # If there is no streams install, get java from JAVA_HOME and use the remote contexts.
        if streams_install is None:
            java_home = os.environ.get('JAVA_HOME')
            if java_home is None:
                raise ValueError("JAVA_HOME not found. Please set the JAVA_HOME system variable")

            jvm = os.path.join(java_home, "bin", "java")
            submit_class = "com.ibm.streamsx.topology.context.remote.RemoteContextSubmit"
        # Otherwise, use the Java version from the streams install
        else:
            jvm = os.path.join(streams_install, "java", "jre", "bin", "java")
            if ConfigParams.FORCE_REMOTE_BUILD in self.config and self.config[ConfigParams.FORCE_REMOTE_BUILD]:
                submit_class = "com.ibm.streamsx.topology.context.remote.RemoteContextSubmit"
            else:
                submit_class = "com.ibm.streamsx.topology.context.StreamsContextSubmit"
            cp = cp + ':' + os.path.join(streams_install, "lib", "com.ibm.streams.operator.samples.jar")

        args = [jvm, '-classpath', cp, submit_class, self.ctxtype, self.fn]
        logger.info("Generating SPL and submitting application.")
        process = subprocess.Popen(args, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=self._get_java_env())
        try:
            stderr_thread = threading.Thread(target=_print_process_stderr, args=([process, self]))
            stderr_thread.daemon = True
            stderr_thread.start()

            stdout_thread = threading.Thread(target=_print_process_stdout, args=([process]))
            stdout_thread.daemon = True
            stdout_thread.start()
            process.wait()

            results_json = None
            try:
                with open(self.results_file) as _file:
                    results_json = json.loads(_file.read())
            except IOError:
                logger.exception("Error opening an reading from results file.")
                raise
            except json.JSONDecodeError:
                logger.exception("Results file doesn't contain valid JSON")
                raise
            except:
                raise

            _delete_json(self)
            results_json['return_code'] = process.returncode
            return results_json

        except:
            logger.exception("Error starting java subprocess for submission")
            raise

    def _get_java_env(self):
        "Get the environment to be passed to the Java execution"
        return dict(os.environ)

    def _add_python_info(self):
        # Python information added to deployment
        pi = {}
        pi["prefix"] = sys.exec_prefix
        pi["version"] = sys.version
        self.config["python"] = pi

    def _create_job_config_overlays(self):
        if ConfigParams.JOB_CONFIG in self.config:
            jco = self.config[ConfigParams.JOB_CONFIG]
            del self.config[ConfigParams.JOB_CONFIG]
            jco._add_overlays(self.config)

    def _create_full_json(self):
        fj = dict()
        fj["deploy"] = self.config
        fj["graph"] = self.graph.generateSPLGraph()

        _file = tempfile.NamedTemporaryFile(prefix="results", suffix=".json", mode="w+t", delete=False)
        _file.close()
        fj["submissionResultsFile"] = _file.name
        self.results_file = _file.name
        logger.debug("Results file created at " + _file.name)

        return fj

    def _create_json_file(self, fj):
        try:
            if sys.hexversion < 0x03000000:
                tf = tempfile.NamedTemporaryFile(mode="w+t", suffix=".json", prefix="splpytmp", delete=False)
            else:
                tf = tempfile.NamedTemporaryFile(mode="w+t", suffix=".json", encoding="UTF-8", prefix="splpytmp",
                                             delete=False)
            tf.write(json.dumps(fj, sort_keys=True, indent=2, separators=(',', ': ')))
            tf.close()
        except Exception:
            logger.exception("Error generating SPL and creating JSON file.")
            raise
        self.fn = tf.name

    def _setup_views(self, graph, submission_result, username, password, rest_api_url):
        # Give each view in the app the necessary information to connect to SWS.
        if graph.get_views():
            connection_info = {'username': username, 'password': password, 'rest_api_url': rest_api_url}
            for view in graph.get_views():
                view.set_streams_connection_config(connection_info)

    # There are two modes for execution.
    #
    # Pypi (Python focused)
    #  Pypi (pip install) package includes the SPL toolkit as
    #      streamsx/.toolkit/com.ibm.streamsx.topology
    #      However the streamsx Python packages have been moved out
    #      of the toolkit's (opt/python/package) compared
    #      to the original toolkit layout. They are moved to the
    #      top level of the pypi package.
    #
    # SPL Toolkit (SPL focused):
    #   Streamsx Python packages are executed from opt/python/packages
    #
    # This function determines the root of the SPL toolkit based
    # upon the existance of the '.toolkit' directory.
    #
    @staticmethod
    def _get_toolkit_root():
        # Directory of this file (streamsx/topology)
        dir = os.path.dirname(os.path.abspath(__file__))

        # This is streamsx
        dir = os.path.dirname(dir)

        # See if .toolkit exists, if so executing from
        # a pip install
        tk_root = os.path.join(dir, '.toolkit', 'com.ibm.streamsx.topology')
        if os.path.isdir(tk_root):
            return tk_root

        # Else dir is tk/opt/python/packages/streamsx

        dir = os.path.dirname(dir)
        dir = os.path.dirname(dir)
        dir = os.path.dirname(dir)
        tk_root = os.path.dirname(dir)
        return tk_root



class _JupyterSubmitter(_BaseSubmitter):
    def submit(self):
        tk_root = self._get_toolkit_root()

        cp = os.path.join(tk_root, "lib", "com.ibm.streamsx.topology.jar")

        # Create the json file containing the representation of the application
        self._create_json_file(self._create_full_json())

        streams_install = os.environ.get('STREAMS_INSTALL')
        # If there is no streams install, get java from JAVA_HOME and use the remote contexts.
        if streams_install is None:
            java_home = os.environ.get('JAVA_HOME')
            if java_home is None:
                raise ValueError("JAVA_HOME not found. Please set the JAVA_HOME system variable")

            jvm = os.path.join(java_home, "bin", "java")
            submit_class = "com.ibm.streamsx.topology.context.remote.RemoteContextSubmit"
        # Otherwise, use the Java version from the streams install
        else:
            jvm = os.path.join(streams_install, "java", "jre", "bin", "java")
            submit_class = "com.ibm.streamsx.topology.context.StreamsContextSubmit"
            cp = cp + ':' + os.path.join(streams_install, "lib", "com.ibm.streams.operator.samples.jar")

        args = [jvm, '-classpath', cp, submit_class, ContextTypes.STANDALONE, self.fn]
        process = subprocess.Popen(args, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            stderr_thread = threading.Thread(target=_print_process_stderr, args=([process, self]))
            stderr_thread.daemon = True
            stderr_thread.start()

            if process.stdout is None:
                raise ValueError("The returned stdout from the spawned process is None.")
            return process.stdout
        except:
            logger.exception("Error starting java subprocess for submission")
            raise

class _StreamingAnalyticsSubmitter(_BaseSubmitter):
    """
    A submitter supports the ANALYTICS_SERVICE (Streaming Analytics service) context.
    """
    def __init__(self, ctxtype, config, graph):
        super(_StreamingAnalyticsSubmitter, self).__init__(ctxtype, config, graph)

        self._set_vcap()
        self._set_credentials()

        # Clear the VCAP_SERVICES key in config, since env var will contain the content
        self._config().pop(ConfigParams.VCAP_SERVICES, None)

        username = self.credentials['userid']
        password = self.credentials['password']

        # Obtain REST only when needed. Otherwise, submitting "Hello World" without requests fails.
        try:
            import requests
        except (ImportError, NameError):
            logger.exception('Unable to import the optional "Requests" module. This is needed when performing'
                             ' a remote build or retrieving view data.')
            raise

        # Obtain the streams SWS REST URL
        resources_url = self.credentials['rest_url'] + self.credentials['resources_path']
        try:
            response = requests.get(resources_url, auth=(username, password)).json()
        except:
            logger.exception("Error while querying url: " + resources_url)
            raise

        rest_api_url = response['streams_rest_url'] + '/resources'

        # Give each view in the app the necessary information to connect to SWS.
        self._setup_views(graph, None, username, password, rest_api_url)

    def _set_vcap(self):
        "Set self.vcap to the VCAP services, from env var or the config"
        self._vcap = rest._get_vcap_services(self._config().get(ConfigParams.VCAP_SERVICES))

    def _set_credentials(self):
        "Set self.credentials for the selected service, from self.vcap"
        self.credentials = rest._get_credentials(self._config().get(ConfigParams.SERVICE_NAME), self._vcap)

    def _get_java_env(self):
        "Pass the VCAP through the environment to the java submission"
        env = super(_StreamingAnalyticsSubmitter, self)._get_java_env()
        env['VCAP_SERVICES'] = json.dumps(self._vcap)
        return env

class _DistributedSubmitter(_BaseSubmitter):
    """
    A submitter which supports the DISTRIBUTED (on-prem cluster) context.
    """
    def __init__(self, ctxtype, config, graph, username, password):
        _BaseSubmitter.__init__(self, ctxtype, config, graph)

        # If a username or password isn't supplied, don't attempt to retrieve view data, but throw an error if views
        # were created
        if (username is None or password is None) and len(graph.get_views()) > 0:
            raise ValueError("To access views data, both a username and a password must be supplied when submitting.")
        elif username is None or password is None:
            return
        try:
            process = subprocess.Popen(['streamtool', 'geturl', '--api'],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            rest_api_url = process.stdout.readline().strip().decode('utf-8')
            logger.debug("The rest API URL obtained from streamtool is " + rest_api_url)
        except:
            logger.exception("Error getting SWS rest api url via streamtool")
            raise

        # Give each view in the app the necessary information to connect to SWS.
        self._setup_views(graph, None, username, password, rest_api_url)


class _SubmitContextFactory(object):
    """
    ContextSubmitter:
        Responsible for performing the correct submission depending on a number of factors, including: the
        presence/absence of a streams install, the type of context, and whether the user seeks to retrieve data via rest
    """
    def __init__(self, graph, config=None, username=None, password=None):
        self.graph = graph
        self.config = config
        self.username = username
        self.password = password

        if self.config is None:
            self.config = {}

    def get_submit_context(self, ctxtype):

        # If there is no streams install present, currently only ANALYTICS_SERVICE, TOOLKIT, and BUILD_ARCHIVE
        # are supported.
        streams_install = os.environ.get('STREAMS_INSTALL')
        if streams_install is None:
            if not (ctxtype == ContextTypes.TOOLKIT or ctxtype == ContextTypes.BUILD_ARCHIVE
                    or ctxtype == ContextTypes.ANALYTICS_SERVICE):
                raise UnsupportedContextException(ctxtype + " must be submitted when a Streams install is present.")

        if ctxtype == ContextTypes.JUPYTER:
            logger.debug("Selecting the JUPYTER context for submission")
            return _JupyterSubmitter(ctxtype, self.config, self.graph)
        elif ctxtype == ContextTypes.DISTRIBUTED:
            logger.debug("Selecting the DISTRIBUTED context for submission")
            return _DistributedSubmitter(ctxtype, self.config, self.graph, self.username, self.password)
        elif ctxtype == ContextTypes.ANALYTICS_SERVICE:
            logger.debug("Selecting the ANALYTICS_SERVICE context for submission")
            if not (sys.version_info.major == 3 and sys.version_info.minor == 5):
                raise RuntimeError("The ANALYTICS_SERVICE context only supports Python version 3.5")
            return _StreamingAnalyticsSubmitter(ctxtype, self.config, self.graph)
        else:
            logger.debug("Using the BaseSubmitter, and passing the context type through to java.")
            return _BaseSubmitter(ctxtype, self.config, self.graph)


# Used to delete the JSON file after it is no longer needed.
def _delete_json(submitter):
    for fn in [submitter.fn, submitter.results_file]:
        if os.path.isfile(fn):
            os.remove(fn)


# Used by a thread which polls a subprocess's stdout and writes it to stdout
def _print_process_stdout(process):
    try:
        while True:
            if sys.version_info.major == 2:
                sout = codecs.getwriter('utf8')(sys.stdout)
            line = process.stdout.readline()
            if len(line) == 0:
                process.stdout.close()
                break
            line = line.decode("utf-8").strip()
            if sys.version_info.major == 2:
                sout.write(line)
                sout.write("\n")
            else:
                print(line)
    except:
        process.stdout.close()
        logger.exception("Error reading from process stdout")
        raise


# Used by a thread which polls a subprocess's stderr and writes it to stderr, until the sc compilation
# has begun.
def _print_process_stderr(process, submitter):
    try:
        if sys.version_info.major == 2:
            serr = codecs.getwriter('utf8')(sys.stderr)
        while True:
            line = process.stderr.readline()
            if len(line) == 0:
                process.stderr.close()
                break
            line = line.decode("utf-8").strip()
            if sys.version_info.major == 2:
                serr.write(line)
                serr.write("\n")
            else:
                print(line)
    except:
        process.stderr.close()
        logger.exception("Error reading from process stderr")
        raise


class UnsupportedContextException(Exception):
    """
    An exeption class for when something goes wrong with submitting using a particular context.
    """
    def __init__(self, msg):
        Exception.__init__(self, msg)

class ContextTypes(object):
    """
        Types of submission contexts:

        DISTRIBUTED - the topology is submitted to a Streams instance.
        The bundle is submitted using `streamtool` which must be setup to submit without requiring authentication
        input. Additionally, a username and password may optionally be provided to enable retrieving data from remote
        views.
        STANDALONE - the topology is executed directly as an Streams standalone application.
        The standalone execution is spawned as a separate process
        BUNDLE - execution of the topology produces an SPL application bundle
        (.sab file) that can be submitted to an IBM Streams instance as a distributed application.
        STANDALONE_BUNDLE - execution of the topology produces an SPL application bundle that, when executed,
        is spawned as a separate process.
        JUPYTER - the topology is run in standalone mode, and context.submit returns a stdout streams of bytes which
        can be read from to visualize the output of the application.
        BUILD_ARCHIVE - Creates a Bluemix-compatible build archive.
        execution of the topology produces a build archive, which can be submitted to a streaming
        analytics Bluemix remote build service.
        TOOLKIT - Execution of the topology produces a toolkit.
        ANALYTICS_SERVICE - If a local Streams install is present, the application is built locally and then submitted
        to a Bluemix streaming analytics service. If a local Streams install is not present, the application is
        submitted to, built, and executed on a Bluemix streaming analytics service. If the ConfigParams.REMOTE_BUILD
        flag is set to true, the application will be built on Bluemix even if a local Streams install is present.
    """
    TOOLKIT = 'TOOLKIT'
    BUILD_ARCHIVE = 'BUILD_ARCHIVE'
    BUNDLE = 'BUNDLE'
    STANDALONE_BUNDLE = 'STANDALONE_BUNDLE'
    STANDALONE = 'STANDALONE'
    DISTRIBUTED = 'DISTRIBUTED'
    JUPYTER = 'JUPYTER'
    ANALYTICS_SERVICE = 'ANALYTICS_SERVICE'


class ConfigParams(object):
    """
    Configuration options which may be used as keys in the submit's config parameter.

    VCAP_SERVICES - a json object containing the VCAP information used to submit to Bluemix
    SERVICE_NAME - the name of the streaming analytics service to use from VCAP_SERVICES.
    """
    VCAP_SERVICES = 'topology.service.vcap'
    SERVICE_NAME = 'topology.service.name'
    FORCE_REMOTE_BUILD = 'topology.forceRemoteBuild'
    JOB_CONFIG = 'topology.jobConfigOverlays'
    """
    Key for a :py:class:`JobConfig` object representing a job configuration for a submission.
    """

class JobConfig(object):
    """
    Job configuration.

    `JobConfig` allows configuration of job that will result from
    submission of a py:class:`Topology` (application).

    A `JobConfig` is set in the `config` dictionary passed to :py:func:`~streamsx.topology.context.submit`
    using the key :py:const:`~ConfigParams.JOB_CONFIG`. :py:meth:`~JobConfig.add` exists as a convenience
    method to add it to a submission configuration.

    Args:
        job_name(str): The name that is assigned to the job. A job name must be unique within a Streasm instance
            When set to `None` a system generated name is used.
        job_group(str): The job group to use to control permissions for the submitted job.
        preload(bool): Specifies whether to preload the job onto all resources in the instance, even if the job is
            not currently needed on each. Preloading the job can improve PE restart performance if the PEs are
            relocated to a new resource.
        data_directory(str): Specifies the location of the optional data directory. The data directory is a path
            within the cluster that is running the Streams instance.
        tracing: Specify the application trace level. See :py:attr:`tracing`

    Example::

        # Submit a job with the name NewsIngester
        cfg = {}
        job_config = JobConfig(job_name='NewsIngester')
        job_config.add(cfg)
        context.submit('ANALYTICS_SERVICE', topo, cfg)
    """
    def __init__(self, job_name=None, job_group=None, preload=False, data_directory=None, tracing=None):
        self.job_name = job_name
        self.job_group = job_group
        self.preload = preload
        self.data_directory = data_directory
        self.tracing = tracing

    @property
    def tracing(self):
        """
        Runtime application trace level.

        The runtime application trace level can be a string with value ``error``, ``warn``, ``info``,
        ``debug`` or ``trace``.

        In addition a level from Python ``logging`` module can be used in with ``CRITICAL`` and ``ERROR`` mapping
        to ``error``, ``WARNING`` to ``warn``, ``INFO`` to ``info`` and ``DEBUG`` to ``debug``.

        Setting tracing to `None` or ``logging.NOTSET`` will result in the job submission using the Streams instance
        application trace level.

        The value of ``tracing`` is the level as a string (``error``, ``warn``, ``info``, ``debug`` or ``trace``)
        or None.

        """
        return self._tracing

    @tracing.setter
    def tracing(self, level):
        if level is None:
            pass
        elif level in {'error', 'warn', 'info', 'debug', 'trace'}:
            pass
        elif level == logging.CRITICAL or level == logging.ERROR:
            level = 'error'
        elif level == logging.WARNING:
            level = 'warn'
        elif level == logging.INFO:
            level = 'info'
        elif level == logging.DEBUG:
            level = 'debug'
        elif level == logging.NOTSET:
            level = None
        else:
            raise ValueError("Tracing value {0} not supported.".format(level))

        self._tracing = level

    def add(self, config):
        """
        Add this `JobConfig` into a submission configuration object.

        Args:
            config(dict): Submission configuration.

        Returns:
            dict: config.

        """
        config[ConfigParams.JOB_CONFIG] = self
        return config

    def _add_overlays(self, config):
        """
        Add this as a jobConfigOverlays JSON to config.
        """
        jco = {}
        config["jobConfigOverlays"] = [jco]
        jc = {}

        if self.job_name is not None:
            jc["jobName"] = self.job_name
        if self.job_group is not None:
            jc["jobGroup"] = self.job_group
        if self.data_directory is not None:
            jc["dataDirectory"] = self.data_directory
        if self.preload:
            jc['preloadApplicationBundles'] = True
        if self.tracing is not None:
            jc['tracing'] = self.tracing

        if jc:
            jco["jobConfig"] = jc
